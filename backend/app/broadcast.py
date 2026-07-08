from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any, AsyncIterator

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .crypto import decrypt
from .db import SessionLocal
from .documents import document_prompt_block
from .models import (
    Attachment,
    GeneratedFile,
    Lane,
    LaneMessage,
    Provider,
    Session as ChatSession,
    ToolCall,
    ToolCredential,
    Turn,
)
from .providers.base import ChatMessage, ToolSpec
from .providers.registry import build_provider
from .tools.argfix import unflatten_args
from .tools.base import ToolContext
from .tools.registry import get_tool, resolve_enabled_tools

# cancellation registry: key (session_id, lane_id) -> asyncio.Event
_cancels: dict[tuple[str, str], asyncio.Event] = {}

# Lane tasks that outlived their SSE stream (the client navigated away / disconnected).
# We keep strong references so the event loop doesn't garbage-collect — and thereby
# cancel — them; they finish in the background and persist their results.
_detached_tasks: set[asyncio.Task] = set()

# Safety cap: how many background (detached) lane tasks may run at once. Beyond this we
# stop detaching and let further disconnected streams cancel instead — a runaway pile of
# concurrent background LLM streams can overload the single dev worker.
MAX_DETACHED_TASKS = 36

# In-memory live progress of currently-generating lanes: (session_id, lane_id) ->
# {"turn_id", "text"}. Lets a client that returns to a chat whose lanes are still running
# (no SSE stream of its own) poll and show the partial answer as it grows, instead of a
# blank spinner. Purely in-memory (no DB churn); cleared when the lane finishes.
_progress: dict[tuple[str, str], dict] = {}


def lane_progress(session_id: str) -> list[dict]:
    """Partial text of lanes currently generating for a session."""
    return [
        {"lane_id": lid, "turn_id": p.get("turn_id"), "text": p.get("text", "")}
        for (sid, lid), p in _progress.items()
        if sid == session_id
    ]


def request_stop(session_id: str, lane_id: str) -> None:
    ev = _cancels.get((session_id, lane_id))
    if ev:
        ev.set()


def active_lane_ids(session_id: str) -> list[str]:
    """Lane ids currently generating for a session (present in the cancel registry)."""
    return [lid for (sid, lid) in _cancels.keys() if sid == session_id]


def active_session_ids() -> set[str]:
    """Session ids with at least one lane currently generating."""
    return {sid for (sid, _lid) in _cancels.keys()}


async def _events_until_cancel(
    stream: AsyncIterator[Any], cancel: asyncio.Event
) -> AsyncIterator[Any]:
    """Yield events from an LLM stream but stop *immediately* when ``cancel`` is set —
    even while awaiting the next event.

    Checking ``cancel.is_set()`` only between yielded events means a Stop click is
    ignored while the provider is producing nothing (first-token latency, an
    inter-token gap, or a stalled connection), which can take many seconds. Racing each
    read against the cancel event makes Stop take effect right away and closes the
    underlying HTTP stream so the provider connection is released.
    """
    it = stream.__aiter__()
    cancel_task: asyncio.Task = asyncio.ensure_future(cancel.wait())
    try:
        while True:
            if cancel.is_set():
                return
            next_task: asyncio.Task = asyncio.ensure_future(it.__anext__())
            done, _pending = await asyncio.wait(
                {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if next_task in done:
                try:
                    yield next_task.result()
                except StopAsyncIteration:
                    return
            else:
                # Cancel fired first — abort the in-flight read and close the stream.
                next_task.cancel()
                try:
                    await next_task
                except BaseException:  # noqa: BLE001
                    pass
                try:
                    await it.aclose()
                except BaseException:  # noqa: BLE001
                    pass
                return
    finally:
        if not cancel_task.done():
            cancel_task.cancel()


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _image_part(db: DbSession, att: Attachment) -> dict | None:
    """Return an OpenAI-format image_url content part for an attachment, or None."""
    if att.kind != "image":
        return None
    path = os.path.join(settings.UPLOAD_DIR, att.storage_path)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        data = fh.read()
    b64 = base64.b64encode(data).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
    }


def _user_content(text: str, image_parts: list[dict]) -> Any:
    """Build OpenAI content: a plain string, or a multimodal list when images exist."""
    if not image_parts:
        return text
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(image_parts)
    return parts


def build_lane_history(
    db: DbSession,
    session: ChatSession,
    lane: Lane,
    up_to_turn_order: int | None = None,
) -> list[ChatMessage]:
    """Reconstruct a lane's conversation up to (but not including) a turn order."""
    messages: list[ChatMessage] = []
    # Combine the user's global custom instructions with the session system prompt.
    from .models import User

    parts: list[str] = []
    user = db.get(User, session.user_id)
    if user and user.custom_instructions and user.custom_instructions.strip():
        parts.append(user.custom_instructions.strip())
    if session.system_prompt and session.system_prompt.strip():
        parts.append(session.system_prompt.strip())
    if parts:
        messages.append({"role": "system", "content": "\n\n".join(parts)})

    turns = db.scalars(
        select(Turn).where(Turn.session_id == session.id).order_by(Turn.order_index)
    ).all()
    for turn in turns:
        if up_to_turn_order is not None and turn.order_index >= up_to_turn_order:
            break
        # was this lane targeted by this turn?
        targets = turn.target_lane_ids_json
        if targets and lane.id not in targets:
            continue
        image_parts = []
        for att in turn.attachments:
            part = _image_part(db, att)
            if part:
                image_parts.append(part)
        text = turn.content + document_prompt_block(turn.attachments)
        messages.append(
            {"role": "user", "content": _user_content(text, image_parts)}
        )
        assistant = db.scalar(
            select(LaneMessage)
            .where(
                LaneMessage.lane_id == lane.id,
                LaneMessage.turn_id == turn.id,
                LaneMessage.role == "assistant",
            )
            .order_by(LaneMessage.order_index)
        )
        if assistant and assistant.content:
            messages.append({"role": "assistant", "content": assistant.content})
    return messages


_GENERATED_LINK_RE = re.compile(
    r"/api/files/([0-9a-f]{32}\.(pptx|docx|xlsx|pdf))\?name=([^)\s]+)"
)
_GEN_MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def _record_generated_files(db: DbSession, session: ChatSession, text: str) -> None:
    """Scan a tool result for generated-file download links and log them so they
    appear in the session's Files library."""
    for stored_name, ext, download_name in _GENERATED_LINK_RE.findall(text or ""):
        exists = db.scalar(
            select(GeneratedFile).where(GeneratedFile.stored_name == stored_name)
        )
        if exists:
            continue
        path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        db.add(
            GeneratedFile(
                user_id=session.user_id,
                session_id=session.id,
                stored_name=stored_name,
                download_name=download_name,
                mime_type=_GEN_MIME.get(ext, "application/octet-stream"),
                size_bytes=size,
                kind=ext,
            )
        )
    db.commit()

def _inject_tool_guidance(messages: list, tools: list) -> None:
    """Steer models to actually USE the file-generation tools instead of writing code.

    Some models (notably gemini) will output python-pptx/docx code and claim they
    "cannot create files" rather than calling generate_pptx/docx/xlsx/pdf. A firm
    system instruction fixes that across providers.
    """
    gen = sorted(
        t.definition.name
        for t in tools
        if t.definition.name.startswith("generate_")
    )
    if not gen:
        return
    guidance = (
        "You have tools that produce REAL, downloadable files: " + ", ".join(gen) + ". "
        "STRICT RULE FOR EVERY MODEL: a downloadable file (PowerPoint/Word/Excel/PDF/image) "
        "is created ONLY when the user EXPLICITLY asks for that file. Two rules govern the "
        "tools:\n"
        "1) ONLY create a file when the user has EXPLICITLY asked for a FILE or DOCUMENT "
        "in a downloadable format — i.e. their message names a document/file or a format "
        "like Word/PowerPoint/Excel/PDF/image (e.g. 'make a PowerPoint', 'export this to "
        "Excel', 'give me a PDF', 'create a Word doc', 'put it in a file', 'download as "
        "pptx') — or they have clearly confirmed they want one. The verbs 'generate', "
        "'create', 'make', 'build', 'write', 'add', or 'give me' do NOT by themselves mean "
        "a file: applied to content they mean produce it INLINE in your reply. For example "
        "'generate the az cli to create this', 'write a script', 'create a function', 'make "
        "a plan', 'generate code', 'add diagrams', 'add a diagram', 'add a chart', 'add a "
        "table', 'draw a flowchart', 'illustrate this' are requests for INLINE content in "
        "the chat (prose, code, a Mermaid diagram, or a Markdown table) — answer them "
        "inline and do NOT call any generate_* tool. In particular, asking for a diagram, "
        "chart, or table is NEVER by itself a request for a PowerPoint/PDF/image file — put "
        "a diagram inline as a ```mermaid block. Never produce a file the user did not "
        "clearly ask for just because the topic seems document-shaped.\n"
        "2) When a document, deck, spreadsheet, or PDF would genuinely make your answer "
        "more useful but the user has NOT asked for one, you MAY add a brief one-line "
        "OFFER at the end (e.g. 'I can turn this into a PowerPoint or Excel file if you "
        "want.') — but do NOT call any generate_* tool yet; wait for them to say yes.\n"
        "When the user DOES ask for a file: you MUST actually call the matching tool "
        "(generate_pptx / generate_docx / generate_xlsx / generate_pdf / generate_image) "
        "— one call per requested file — gathering any needed data first, then reply "
        "with the download link(s) the tool returns. Do NOT output code (e.g. "
        "python-pptx/openpyxl) to build the file, and never claim you cannot create, "
        "compile, or host files — these tools do it for you."
    )
    # Strong prefix in the system message.
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        base = messages[0].get("content") or ""
        sep = "\n\n" if base else ""
        messages[0] = {**messages[0], "content": f"{base}{sep}{guidance}"}
    else:
        messages.insert(0, {"role": "system", "content": guidance})
    # A fresh reminder appended to the latest user message (weaker models weight
    # recency); attached as text so it survives multimodal content too.
    reminder = (
        "\n\n[System reminder: Only generate a file if THIS message explicitly asks for a "
        "FILE/document or a format (Word/PowerPoint/Excel/PDF/image). Verbs like "
        "'generate', 'create', 'make', 'add', or 'write' applied to code/commands/CLI/text/"
        "diagrams/charts/tables mean produce it INLINE here — NOT a file (e.g. 'generate "
        "the az cli' = show commands in chat; 'add diagrams' = add inline ```mermaid "
        "diagrams, NOT a PowerPoint/PDF/image). If it does ask for a file, you MUST call "
        "the matching generate_* tool now and return the download link (not only prose or "
        "code). If it does NOT ask for a file, do NOT create one; at most add a brief "
        "one-line offer to make a PowerPoint/Word/Excel/PDF if they'd like it.]"
    )
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                messages[i] = {**m, "content": content + reminder}
            elif isinstance(content, list):
                messages[i] = {
                    **m,
                    "content": content + [{"type": "text", "text": reminder}],
                }
            break

def _inject_diagram_guidance(messages: list) -> None:
    """Tell models to draw diagrams as Mermaid, which the app renders as real visuals.

    Without this, models "add a diagram" by drawing ASCII-art boxes inside a plain code
    fence — which shows up as an unhelpful black text block. The frontend renders
    ```mermaid fenced blocks into actual SVG diagrams, so steer models there.
    """
    guidance = (
        "DIAGRAMS: When the user asks for a diagram, flowchart, architecture/system "
        "diagram, sequence diagram, ER diagram, mind map, state machine, or any visual, "
        "output it INLINE as a fenced ```mermaid code block containing VALID Mermaid syntax "
        "— the app renders Mermaid as a real, rendered diagram. Do NOT draw diagrams with "
        "ASCII art, plain-text boxes, or +---+ characters, and do NOT put the diagram in a "
        "plain (non-mermaid) code fence. A request for a diagram/chart (e.g. 'add "
        "diagrams') is NOT a request for a PowerPoint/PDF/image file — render it inline "
        "with Mermaid and do NOT call any generate_* tool unless the user explicitly asked "
        "for a file in a specific format. Choose the right Mermaid type (e.g. 'flowchart "
        "LR/TD', 'sequenceDiagram', 'erDiagram', 'stateDiagram-v2', 'mindmap'), keep node "
        "labels short, and wrap labels containing special characters in quotes. You may add "
        "a short explanation before or after the diagram."
    )
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        base = messages[0].get("content") or ""
        sep = "\n\n" if base else ""
        messages[0] = {**messages[0], "content": f"{base}{sep}{guidance}"}
    else:
        messages.insert(0, {"role": "system", "content": guidance})


def _session_documents(db: DbSession, session: ChatSession) -> list[dict]:
    """Collect extracted text of all document attachments across the session's turns,
    so the read_document tool can access any uploaded document."""
    docs: list[dict] = []
    seen: set[str] = set()
    turns = db.scalars(
        select(Turn).where(Turn.session_id == session.id).order_by(Turn.order_index)
    ).all()
    for turn in turns:
        for att in turn.attachments:
            if att.kind == "document" and att.extracted_text and att.filename not in seen:
                seen.add(att.filename)
                docs.append({"name": att.filename, "text": att.extracted_text})
    return docs


def _brave_key(db: DbSession, user_id: str) -> str | None:
    cred = db.scalar(
        select(ToolCredential).where(
            ToolCredential.user_id == user_id, ToolCredential.tool == "web_search"
        )
    )
    return decrypt(cred.api_key_encrypted) if cred else None


def _search_engine(db: DbSession, user_id: str) -> str | None:
    """Read the user's preferred web_search engine (brave|duckduckgo) from the
    web_search tool credential's extra config."""
    cred = db.scalar(
        select(ToolCredential).where(
            ToolCredential.user_id == user_id, ToolCredential.tool == "web_search"
        )
    )
    if cred and cred.extra_json:
        return cred.extra_json.get("engine")
    return None


def _image_provider(db: DbSession, user_id: str) -> dict:
    """Best-effort: use the user's first API-key OpenAI/OpenAI-compatible provider
    for the generate_image tool."""
    prov = db.scalar(
        select(Provider).where(
            Provider.user_id == user_id,
            Provider.provider_type.in_(["openai", "openai_compatible"]),
            Provider.auth_method == "api_key",
        )
    )
    if not prov:
        return {}
    extra = prov.extra_json or {}
    return {
        "image_api_key": decrypt(prov.api_key_encrypted),
        "image_base_url": prov.base_url or None,
        "image_model": extra.get("image_model"),
    }


async def run_lane(
    session_id: str,
    lane_id: str,
    turn_id: str,
    user_message: ChatMessage,
    queue: asyncio.Queue,
) -> None:
    """Run one lane's agent loop, pushing SSE strings onto the shared queue."""
    cancel = asyncio.Event()
    _cancels[(session_id, lane_id)] = cancel
    db = SessionLocal()
    started = time.monotonic()
    full_text = ""
    persisted = False
    error: str | None = None
    tool_call_rows: list[tuple[str, dict, Any]] = []
    try:
        session = db.get(ChatSession, session_id)
        lane = db.get(Lane, lane_id)
        turn = db.get(Turn, turn_id)
        if not session or not lane or not turn:
            raise RuntimeError("Session/lane/turn not found")
        provider = db.get(Provider, lane.provider_id)
        if not provider:
            raise RuntimeError("Provider not found")

        lane.state = "streaming"
        db.add(lane)
        # Run the blocking write off the event loop so a slow/locked SQLite commit can't
        # stall every other lane's streaming on the single worker.
        await asyncio.to_thread(db.commit)

        await queue.put(sse("lane_start", {"lane_id": lane_id, "turn_id": turn_id}))

        history = build_lane_history(db, session, lane, up_to_turn_order=turn.order_index)
        messages: list[ChatMessage] = [*history, user_message]

        tools = resolve_enabled_tools(session.tool_config_json) if session.tools_enabled else []
        _inject_tool_guidance(messages, tools)
        _inject_diagram_guidance(messages)
        tool_specs = [
            ToolSpec(
                name=t.definition.name,
                description=t.definition.description,
                parameters=t.definition.parameters,
            )
            for t in tools
        ]
        ctx = ToolContext(
            user_id=session.user_id,
            brave_api_key=_brave_key(db, session.user_id),
            search_engine=_search_engine(db, session.user_id),
            options=(session.tool_config_json or {}).get("options"),
            documents=_session_documents(db, session),
            **_image_provider(db, session.user_id),
        )

        llm = await build_provider(provider, db, lane.model)
        prompt_tokens = 0
        completion_tokens = 0
        iters = 0
        _call_cache: dict[str, tuple] = {}
        _gen_count = {"n": 0}
        while True:
            iters += 1
            requested_calls = []
            iter_text = ""
            async for ev in _events_until_cancel(
                llm.stream(messages, tool_specs or None), cancel
            ):
                if cancel.is_set():
                    break
                if ev.type == "status" and ev.text:
                    await queue.put(
                        sse(
                            "lane_status",
                            {"lane_id": lane_id, "phase": ev.phase, "text": ev.text},
                        )
                    )
                elif ev.type == "token" and ev.text:
                    iter_text += ev.text
                    full_text += ev.text
                    _progress[(session_id, lane_id)] = {
                        "turn_id": turn_id,
                        "text": full_text,
                    }
                    await queue.put(
                        sse("chunk", {"lane_id": lane_id, "delta": ev.text})
                    )
                elif ev.type == "tool_calls":
                    requested_calls.extend(ev.tool_calls)
                elif ev.type == "done":
                    prompt_tokens += ev.prompt_tokens
                    completion_tokens += ev.completion_tokens
            if cancel.is_set():
                break
            if not requested_calls or iters > settings.MAX_TOOL_ITERS:
                break
            # Append the assistant's tool-call turn in native OpenAI format so the
            # follow-up call has proper context (matched by tool_call_id).
            messages.append(
                {
                    "role": "assistant",
                    "content": iter_text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in requested_calls
                    ],
                }
            )
            for call in requested_calls:
                await queue.put(
                    sse(
                        "tool_call",
                        {
                            "lane_id": lane_id,
                            "tool_call_id": call.id,
                            "tool": call.name,
                            "arguments": call.arguments,
                        },
                    )
                )

            # Execute this turn's tool calls concurrently (bounded) so a model that
            # requests many calls at once doesn't run them one-at-a-time.
            sem = asyncio.Semaphore(settings.MAX_TOOL_CONCURRENCY)

            async def _run_call(call: Any) -> tuple[Any, str, Any, str]:
                async with sem:
                    tool = get_tool(call.name)
                    if not tool:
                        return call, f"Unknown tool: {call.name}", None, "error"
                    # Repair flattened nested arguments (some models emit dotted keys
                    # like "sheets[0].chart.title" instead of nested objects).
                    call_args = unflatten_args(call.arguments)
                    is_generate = call.name.startswith("generate_")
                    # Hard cap on file-generating calls so a looping model can't spawn
                    # dozens of duplicate files.
                    if is_generate and _gen_count["n"] >= settings.MAX_GENERATE_CALLS:
                        return call, (
                            "File-generation limit reached — you have already created "
                            "the maximum number of files. Do NOT call any generate_* "
                            "tool again; reply to the user now with the download links "
                            "from your previous tool results."
                        ), None, "ok"
                    # De-duplicate repeated identical generate_* calls so a looping
                    # model doesn't produce many copies of the same file.
                    cache_key = None
                    if is_generate:
                        try:
                            cache_key = call.name + "|" + json.dumps(
                                call_args, sort_keys=True, default=str
                            )
                        except Exception:  # noqa: BLE001
                            cache_key = None
                        if cache_key and cache_key in _call_cache:
                            cached = _call_cache[cache_key]
                            return call, cached[0], cached[1], "ok"
                    try:
                        res = await tool.run(call_args, ctx)
                        if is_generate:
                            _gen_count["n"] += 1
                        if cache_key is not None:
                            _call_cache[cache_key] = (res.content, res.citations)
                        return call, res.content, res.citations, "ok"
                    except Exception as exc:  # noqa: BLE001
                        return call, f"Tool error: {exc}", None, "error"

            results = await asyncio.gather(*(_run_call(c) for c in requested_calls))

            for call, result_text, citations, status in results:
                tool_call_rows.append(
                    (call.name, call.arguments, {"result": result_text, "citations": citations})
                )
                if status == "ok":
                    try:
                        _record_generated_files(db, session, result_text)
                    except Exception:  # noqa: BLE001
                        db.rollback()
                await queue.put(
                    sse(
                        "tool_result",
                        {
                            "lane_id": lane_id,
                            "tool_call_id": call.id,
                            "status": status,
                            "result": result_text,
                            "citations": citations,
                        },
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result_text,
                    }
                )

        # If the model exhausted the tool loop (or only emitted tool calls) without
        # writing a final answer, force one more completion with tools disabled so the
        # user still gets a response (e.g. the generated download link) instead of an
        # empty message. Some models (e.g. gpt-5.5, gemini) keep calling a generate_*
        # tool until MAX_TOOL_ITERS without ever producing text.
        if not cancel.is_set() and not full_text.strip() and tool_call_rows:
            try:
                final_messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "Write your final answer for the user now, in plain text. "
                            "Do not call any more tools. If you created any files, "
                            "include their download links from the tool results."
                        ),
                    }
                ]
                async for ev in _events_until_cancel(
                    llm.stream(final_messages, None), cancel
                ):
                    if cancel.is_set():
                        break
                    if ev.type == "token" and ev.text:
                        full_text += ev.text
                        await queue.put(
                            sse("chunk", {"lane_id": lane_id, "delta": ev.text})
                        )
                    elif ev.type == "done":
                        completion_tokens += ev.completion_tokens
            except Exception:  # noqa: BLE001
                pass

        # Last-resort fallback: surface the most recent tool result so the user at
        # least gets the generated file link rather than a blank response.
        if not full_text.strip() and tool_call_rows:
            for _name, _args, _res in reversed(tool_call_rows):
                txt = (_res or {}).get("result")
                if txt:
                    full_text = str(txt)
                    await queue.put(
                        sse("chunk", {"lane_id": lane_id, "delta": full_text})
                    )
                    break

        # Guarantee generated-file download links reach the user: if any generate_*
        # tool produced a file but the model's answer doesn't include the link (weaker
        # models often omit it), append the unique links to the response.
        if "/api/files/" not in full_text:
            seen: set[str] = set()
            links: list[str] = []
            for _name, _args, _res in tool_call_rows:
                txt = (_res or {}).get("result") or ""
                for m in re.finditer(r"\[([^\]]+)\]\((/api/files/[^)\s]+)\)", txt):
                    label, url = m.group(1), m.group(2)
                    if url not in seen:
                        seen.add(url)
                        links.append(f"[{label}]({url})")
            if links:
                addition = ("\n\n" if full_text.strip() else "") + (
                    "**Generated file(s):**\n\n" + "\n\n".join(links)
                )
                full_text += addition
                await queue.put(sse("chunk", {"lane_id": lane_id, "delta": addition}))

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = {
            "prompt_tokens": prompt_tokens or None,
            "completion_tokens": completion_tokens or max(1, len(full_text) // 4),
        }
        # The lane or turn can be deleted mid-generation (regenerate, edit-and-resend,
        # closing/removing a lane, or emptying trash). Re-check against the database
        # before persisting so a slow response doesn't crash on a foreign-key
        # violation — discard the orphaned result cleanly instead.
        db.expire_all()
        lane = db.get(Lane, lane_id)
        turn = db.get(Turn, turn_id)
        if lane is None or turn is None:
            await queue.put(
                sse(
                    "lane_error",
                    {
                        "lane_id": lane_id,
                        "detail": "Result discarded — this lane or turn was removed during generation.",
                    },
                )
            )
            return
        lm = LaneMessage(
            lane_id=lane_id,
            turn_id=turn_id,
            role="assistant",
            content=full_text,
            order_index=turn.order_index,
            usage_json=usage,
            latency_ms=latency_ms,
            cost_usd=0.0,
            error=None,
        )
        db.add(lm)
        await asyncio.to_thread(db.flush)
        for name, args, result in tool_call_rows:
            db.add(
                ToolCall(
                    lane_message_id=lm.id,
                    tool_name=name,
                    arguments_json=args,
                    result_json={"result": result.get("result")},
                    citations_json=result.get("citations"),
                    status="ok",
                )
            )
        lane.state = "done"
        db.add(lane)
        # Persist off the event loop — this is the heaviest write (message + tool calls +
        # citations) and runs for every lane completion.
        await asyncio.to_thread(db.commit)
        persisted = True
        await asyncio.to_thread(db.refresh, lm)

        await queue.put(
            sse(
                "lane_done",
                {
                    "lane_id": lane_id,
                    "message": {"id": lm.id, "content": full_text},
                    "usage": usage,
                    "latency_ms": latency_ms,
                    "cost_usd": 0.0,
                },
            )
        )
    except asyncio.CancelledError:
        # The task was cancelled — the client disconnected or the stream was interrupted
        # by a new request (multiplex cancels lane tasks on client disconnect). Persist
        # the partial answer synchronously so it isn't lost, then re-raise to honour the
        # cancellation. No awaits here: the event loop is tearing this task down.
        if not persisted and full_text.strip():
            try:
                db.rollback()
                db.expire_all()
                lane = db.get(Lane, lane_id)
                turn = db.get(Turn, turn_id)
                if lane is not None and turn is not None:
                    db.add(
                        LaneMessage(
                            lane_id=lane_id,
                            turn_id=turn_id,
                            role="assistant",
                            content=full_text,
                            order_index=turn.order_index,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            cost_usd=0.0,
                        )
                    )
                    lane.state = "done"
                    db.add(lane)
                    db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        raise
    except IntegrityError:
        # Lane/turn vanished between the existence check and commit — discard cleanly.
        db.rollback()
        await queue.put(
            sse(
                "lane_error",
                {
                    "lane_id": lane_id,
                    "detail": "Result discarded — this lane or turn was removed during generation.",
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        db.rollback()
        try:
            lane = db.get(Lane, lane_id)
            turn = db.get(Turn, turn_id)
            if lane:
                lane.state = "error"
                db.add(lane)
            if turn:
                db.add(
                    LaneMessage(
                        lane_id=lane_id,
                        turn_id=turn_id,
                        role="assistant",
                        content=full_text,
                        order_index=turn.order_index if turn else 0,
                        error=error,
                    )
                )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        await queue.put(sse("lane_error", {"lane_id": lane_id, "detail": error}))
    finally:
        _cancels.pop((session_id, lane_id), None)
        _progress.pop((session_id, lane_id), None)
        # Safety net: never leave a lane stuck in "streaming"/"thinking" after this task
        # exits. If the stream was cancelled before producing any text (e.g. the client
        # disconnected during the first-token wait), none of the branches above reset the
        # state, and the lane would look frozen forever. Reset it with a fresh session so
        # it works even if `db` is in a broken/rolled-back state.
        try:
            fixdb = SessionLocal()
            try:
                ln = fixdb.get(Lane, lane_id)
                if ln is not None and ln.state in ("streaming", "thinking"):
                    ln.state = "idle"
                    fixdb.add(ln)
                    fixdb.commit()
            finally:
                fixdb.close()
        except Exception:  # noqa: BLE001
            pass
        db.close()


# Per-run event hub: buffers every SSE event of an in-flight broadcast turn and fans it
# out to live subscribers. Lets a client that navigated away and returned re-attach to the
# live stream — replaying what was already emitted, then tailing the rest token-by-token —
# instead of only polling partial text. Keyed by (session_id, turn_id). Kept a short while
# after the run finishes so a returning client can still catch the tail + terminal "done".
HUB_TTL_SECONDS = 45

# The hub's event buffer is also mirrored (batched) to a small NDJSON file under this dir, so
# a client can still replay a run whose in-memory hub was lost to a backend --reload/restart.
_RUNS_DIR = os.path.join(settings.UPLOAD_DIR, "runs")
_RUN_FILE_MAX_AGE = 3600  # startup sweep deletes run buffers older than this (crash leftovers)

_hubs: dict[tuple[str, str], "_RunHub"] = {}


def _run_file_path(session_id: str, turn_id: str) -> str:
    safe = f"{session_id}__{turn_id}".replace("/", "_").replace("\\", "_")
    return os.path.join(_RUNS_DIR, f"{safe}.ndjson")


def _delete_run_file(session_id: str, turn_id: str) -> None:
    try:
        os.remove(_run_file_path(session_id, turn_id))
    except OSError:
        pass


def _sweep_stale_run_files() -> None:
    """Remove run buffers left behind by crashes/old restarts (older than the max age).
    Recent files are kept so an in-progress run interrupted by a --reload can still resume."""
    try:
        now = time.time()
        for name in os.listdir(_RUNS_DIR):
            path = os.path.join(_RUNS_DIR, name)
            try:
                if now - os.path.getmtime(path) > _RUN_FILE_MAX_AGE:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


class _RunHub:
    """In-memory buffer + pub/sub for one broadcast turn's SSE events, mirrored to disk."""

    def __init__(self, session_id: str, turn_id: str) -> None:
        self.events: list[str] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self._path = _run_file_path(session_id, turn_id)
        self._flushed = 0  # events already written to disk
        self._last_flush = 0.0

    async def put(self, item: str) -> None:
        # Duck-types asyncio.Queue.put so run_lane can publish through it unchanged.
        self.events.append(item)
        for q in list(self.subscribers):
            q.put_nowait(item)
        # Batched, off-thread mirror to disk (throttled so token chunks don't hammer I/O).
        if len(self.events) - self._flushed >= 25 or (
            time.time() - self._last_flush > 1.5
        ):
            await asyncio.to_thread(self._flush_sync)

    def _flush_sync(self) -> None:
        new = self.events[self._flushed :]
        if not new:
            return
        try:
            os.makedirs(_RUNS_DIR, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                for e in new:
                    f.write(json.dumps(e))
                    f.write("\n")
            self._flushed = len(self.events)
            self._last_flush = time.time()
        except OSError:
            pass

    def finish(self) -> None:
        self.done = True
        self._flush_sync()  # final small write to capture the terminal "done"
        for q in list(self.subscribers):
            q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:  # replay everything emitted so far
            q.put_nowait(e)
        if self.done:
            q.put_nowait(None)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


def has_hub(session_id: str, turn_id: str) -> bool:
    return (session_id, turn_id) in _hubs


def _read_run_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


async def resume_stream(session_id: str, turn_id: str) -> AsyncIterator[str]:
    """Re-attach to an in-flight broadcast: replay buffered events, then tail live ones."""
    hub = _hubs.get((session_id, turn_id))
    if hub is not None:
        q = hub.subscribe()
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            hub.unsubscribe(q)
        return
    # No live hub (most likely the backend restarted mid-run) — replay the persisted buffer
    # if one exists. The run itself is gone, so this yields the captured partial then closes;
    # the client's reconcile/poll + the persisted DB message take over from there.
    path = _run_file_path(session_id, turn_id)
    if not os.path.exists(path):
        return
    try:
        lines = await asyncio.to_thread(_read_run_lines, path)
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


async def multiplex(
    session_id: str,
    turn_id: str,
    lanes: list[tuple[str, ChatMessage]],
) -> AsyncIterator[str]:
    """Run N lanes concurrently, multiplexing their SSE events over one stream."""
    hub = _RunHub(session_id, turn_id)
    _hubs[(session_id, turn_id)] = hub

    async def _wrap(lane_id: str, msg: ChatMessage) -> None:
        await run_lane(session_id, lane_id, turn_id, msg, hub)

    tasks = [asyncio.create_task(_wrap(lid, msg)) for lid, msg in lanes]

    async def _sentinel() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        # Publish the terminal event through the hub so both the live client and any
        # re-attached (resumed) client receive it. Schedule TTL cleanup BEFORE finish()
        # so the hub can't be orphaned if this task is cancelled right after releasing
        # subscribers.
        await hub.put(sse("done", {"turn_id": turn_id}))

        def _cleanup_hub() -> None:
            _hubs.pop((session_id, turn_id), None)
            _delete_run_file(session_id, turn_id)

        asyncio.get_event_loop().call_later(HUB_TTL_SECONDS, _cleanup_hub)
        hub.finish()

    watcher = asyncio.create_task(_sentinel())
    q = hub.subscribe()
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        hub.unsubscribe(q)
        # If the stream is torn down while lanes are still generating — most commonly the
        # user navigated to another chat, closing the SSE connection — do NOT cancel the
        # in-flight lanes. Detaching them (keeping a reference so they aren't GC'd) lets
        # each response finish and persist in the background, so returning to the chat
        # shows the completed answer instead of a cancelled one. A real Stop still works
        # via request_stop().
        running = [t for t in tasks if not t.done()]
        if not running:
            # Normal completion: the sentinel already published "done", finished the hub,
            # and scheduled TTL cleanup. A returning client can still resume the buffered
            # tail until the TTL elapses. Nothing to tear down here.
            pass
        elif len(_detached_tasks) < MAX_DETACHED_TASKS:
            for t in running:
                _detached_tasks.add(t)
                t.add_done_callback(_detached_tasks.discard)
            # Keep the sentinel alive too. Its asyncio.gather() holds references to the
            # lane tasks — cancelling it would cancel the gather and thereby CANCEL the
            # very lanes we just detached. Let it run so it awaits their completion and
            # keeps publishing to the hub for a resumed client.
            _detached_tasks.add(watcher)
            watcher.add_done_callback(_detached_tasks.discard)
        else:
            # Overload protection: too many background tasks already running. Stop
            # detaching — cancel the lanes + sentinel and tear the hub down so it can't
            # leak (no one will publish its "done").
            for t in running:
                t.cancel()
            watcher.cancel()
            hub.finish()
            _hubs.pop((session_id, turn_id), None)
            _delete_run_file(session_id, turn_id)
