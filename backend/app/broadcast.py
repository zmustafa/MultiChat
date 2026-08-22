from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from .artifact_links import (
    GENERATOR_EXTENSIONS,
    collect_real_links,
    reconcile_generated_links,
    record_generated_files,
    requested_file_generator,
    tool_created_requested_file,
)
from .config import settings
from .crypto import decrypt
from .db import SessionLocal
from .documents import document_prompt_block
from .models import (
    Attachment,
    Lane,
    LaneMessage,
    Provider,
    ToolCall,
    ToolCredential,
    Turn,
)
from .models import (
    Session as ChatSession,
)
from .prompts import inject_diagram_guidance, inject_tool_guidance
from .providers.base import ChatMessage, ToolSpec
from .providers.registry import build_provider
from .run_hub import (
    HUB_TTL_SECONDS,
    _delete_run_file,
    _hubs,
    _RunHub,
    _sweep_stale_run_files,  # noqa: F401 - re-exported for the startup sweep in main.py
    has_hub,
    resume_stream,
    sse,
)
from .tools.argfix import unflatten_args
from .tools.base import ToolContext
from .tools.registry import get_tool, resolve_enabled_tools

# Re-exported so existing imports (`from .broadcast import sse, resume_stream, …`) and the
# startup sweep keep working now that the hub lives in its own module.
__all__ = [
    "HUB_TTL_SECONDS",
    "active_lane_ids",
    "active_session_ids",
    "build_lane_history",
    "has_hub",
    "lane_progress",
    "load_session_turns",
    "multiplex",
    "request_stop",
    "resume_stream",
    "run_lane",
    "sse",
]

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


# Base64-encoded image attachments, shared across every lane and turn of a broadcast.
# Re-reading and re-encoding each image per lane per turn was the single largest source
# of blocking file I/O on the streaming path. Bounded by total bytes, not entry count,
# because one attachment can be several MB.
_IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024
_image_cache: OrderedDict[tuple[str, int, int], dict] = OrderedDict()
_image_cache_bytes = 0


def _image_part(db: DbSession, att: Attachment) -> dict | None:
    """Return an OpenAI-format image_url content part for an attachment, or None."""
    global _image_cache_bytes
    if att.kind != "image":
        return None
    path = os.path.join(settings.UPLOAD_DIR, att.storage_path)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (att.storage_path, int(stat.st_mtime), stat.st_size)
    cached = _image_cache.get(key)
    if cached is not None:
        _image_cache.move_to_end(key)
        return cached
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    b64 = base64.b64encode(data).decode()
    part = {
        "type": "image_url",
        "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
    }
    _image_cache[key] = part
    _image_cache_bytes += len(b64)
    while _image_cache_bytes > _IMAGE_CACHE_MAX_BYTES and len(_image_cache) > 1:
        _, evicted = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(evicted["image_url"]["url"])
    return part


def _user_content(text: str, image_parts: list[dict]) -> Any:
    """Build OpenAI content: a plain string, or a multimodal list when images exist."""
    if not image_parts:
        return text
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(image_parts)
    return parts


def load_session_turns(db: DbSession, session_id: str) -> list[Turn]:
    """Every turn of a session with its attachments eager-loaded.

    ``turn.attachments`` is a lazy relationship, so walking turns without this costs one
    extra query per turn — per lane, per broadcast.
    """
    return list(
        db.scalars(
            select(Turn)
            .where(Turn.session_id == session_id)
            .options(selectinload(Turn.attachments))
            .order_by(Turn.order_index)
        ).all()
    )


def build_lane_history(
    db: DbSession,
    session: ChatSession,
    lane: Lane,
    up_to_turn_order: int | None = None,
    turns: list[Turn] | None = None,
) -> list[ChatMessage]:
    """Reconstruct a lane's conversation up to (but not including) a turn order.

    ``turns`` may be supplied by the caller so that a broadcast fanning out to N lanes
    loads the session's turns once rather than N times.
    """
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

    if turns is None:
        turns = load_session_turns(db, session.id)

    # One query for the whole lane instead of one per turn. Ordered by order_index so the
    # first row for a turn wins, matching the previous per-turn `.order_by(...)` scalar.
    assistant_by_turn: dict[str, str] = {}
    for turn_id, content in db.execute(
        select(LaneMessage.turn_id, LaneMessage.content)
        .where(LaneMessage.lane_id == lane.id, LaneMessage.role == "assistant")
        .order_by(LaneMessage.order_index)
    ).all():
        assistant_by_turn.setdefault(turn_id, content)

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
        answer = assistant_by_turn.get(turn.id)
        if answer:
            messages.append({"role": "assistant", "content": answer})
    return messages


def _session_documents(
    db: DbSession, session: ChatSession, turns: list[Turn] | None = None
) -> list[dict]:
    """Collect extracted text of all document attachments across the session's turns,
    so the read_document tool can access any uploaded document."""
    docs: list[dict] = []
    seen: set[str] = set()
    if turns is None:
        turns = load_session_turns(db, session.id)
    for turn in turns:
        for att in turn.attachments:
            if att.kind == "document" and att.extracted_text and att.filename not in seen:
                seen.add(att.filename)
                docs.append({"name": att.filename, "text": att.extracted_text})
    return docs


def _web_search_credential(db: DbSession, user_id: str) -> tuple[str | None, str | None]:
    """The user's web_search API key and preferred engine, from a single row read.

    These used to be two functions issuing the same query for the same row, once per lane
    per broadcast.
    """
    cred = db.scalar(
        select(ToolCredential).where(
            ToolCredential.user_id == user_id, ToolCredential.tool == "web_search"
        )
    )
    if not cred:
        return None, None
    engine = (cred.extra_json or {}).get("engine")
    return decrypt(cred.api_key_encrypted), engine


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
    shared: dict | None = None,
) -> None:
    """Run one lane's agent loop, pushing SSE strings onto the shared queue.

    ``shared`` is a per-broadcast scratch dict holding values that are identical for every
    lane (tool credentials, extracted documents, the image provider). Only plain data goes
    in it — each lane owns its own DB session, so ORM instances must not be shared.
    """
    cancel = asyncio.Event()
    _cancels[(session_id, lane_id)] = cancel
    db = SessionLocal()
    started = time.monotonic()
    full_text = ""
    ttft_ms: int | None = None
    persisted = False
    error: str | None = None
    tool_call_rows: list[tuple[str, dict, Any]] = []
    if shared is None:
        shared = {}
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

        # History reconstruction is synchronous SQLAlchemy plus (cached) file reads; run
        # it off the loop so a long transcript can't stall the other lanes' streaming.
        turns = await asyncio.to_thread(load_session_turns, db, session_id)
        history = await asyncio.to_thread(
            build_lane_history, db, session, lane, turn.order_index, turns
        )
        messages: list[ChatMessage] = [*history, user_message]

        tools = resolve_enabled_tools(session.tool_config_json) if session.tools_enabled else []
        inject_tool_guidance(messages, tools)
        inject_diagram_guidance(messages)
        tool_specs = [
            ToolSpec(
                name=t.definition.name,
                description=t.definition.description,
                parameters=t.definition.parameters,
            )
            for t in tools
        ]
        if "tool_env" not in shared:
            brave_key, engine = _web_search_credential(db, session.user_id)
            shared["tool_env"] = {
                "brave_api_key": brave_key,
                "search_engine": engine,
                "documents": _session_documents(db, session, turns),
                **_image_provider(db, session.user_id),
            }
        ctx = ToolContext(
            user_id=session.user_id,
            options=(session.tool_config_json or {}).get("options"),
            **shared["tool_env"],
        )

        llm = await build_provider(provider, db, lane.model)
        prompt_tokens = 0
        completion_tokens = 0
        iters = 0
        requested_generator = requested_file_generator(user_message)
        artifact_repair_attempts = 0
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
                    if ttft_ms is None:
                        ttft_ms = int((time.monotonic() - started) * 1000)
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
            if not requested_calls:
                requested_file_created = tool_created_requested_file(
                    tool_call_rows, requested_generator
                )
                matching_tool = next(
                    (spec for spec in tool_specs if spec.name == requested_generator),
                    None,
                )
                if (
                    not requested_file_created
                    and matching_tool is not None
                    and artifact_repair_attempts < 2
                    and iters <= settings.MAX_TOOL_ITERS
                ):
                    artifact_repair_attempts += 1
                    if iter_text.strip():
                        messages.append({"role": "assistant", "content": iter_text})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The current request explicitly requires a downloadable "
                                f"{GENERATOR_EXTENSIONS[requested_generator].upper()} file, "
                                "but no real file has been created. Call the available "
                                f"{requested_generator} tool now. Do not invent, reuse, or "
                                "write an /api/files/ URL yourself; only the tool result can "
                                "provide a valid download link."
                            ),
                        }
                    )
                    tool_specs = [matching_tool]
                    full_text = ""
                    _progress[(session_id, lane_id)] = {
                        "turn_id": turn_id,
                        "text": "",
                    }
                    continue
                break
            if iters > settings.MAX_TOOL_ITERS:
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
            # requests many calls at once doesn't run them one-at-a-time. The semaphore
            # is bound as a default argument rather than captured, so each iteration's
            # tasks provably use that iteration's semaphore.
            sem = asyncio.Semaphore(settings.MAX_TOOL_CONCURRENCY)

            async def _run_call(
                call: Any, sem: asyncio.Semaphore = sem
            ) -> tuple[Any, str, Any, str]:
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
                        if is_generate and "/api/files/" in res.content:
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
                        await asyncio.to_thread(
                            record_generated_files, db, session, result_text
                        )
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
                        if ttft_ms is None:
                            ttft_ms = int((time.monotonic() - started) * 1000)
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

        # Reconcile generated-file download links. The generate_* tools return the real
        # download URL (/api/files/<id>?name=...), but some models (e.g. gpt-5.6) either
        # omit it or fabricate a bogus link (a chat/localhost URL) in its place. Collect
        # the real links from the tool results, then (a) rewrite any fabricated download
        # links to point at the real files and (b) append links the model dropped.
        real_links = collect_real_links(tool_call_rows)

        full_text, missing_links = reconcile_generated_links(full_text, real_links)
        if missing_links:
            links = [f"[{label}]({url})" for label, url in missing_links]
            addition = ("\n\n" if full_text.strip() else "") + (
                "**Generated file(s):**\n\n" + "\n\n".join(links)
            )
            full_text += addition
            await queue.put(sse("chunk", {"lane_id": lane_id, "delta": addition}))

        if not tool_created_requested_file(tool_call_rows, requested_generator):
            notice = (
                "\n\n" if full_text.strip() else ""
            ) + "The requested downloadable file could not be created. No download was produced."
            full_text += notice
            await queue.put(sse("chunk", {"lane_id": lane_id, "delta": notice}))

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
            ttft_ms=ttft_ms,
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
                    "ttft_ms": ttft_ms,
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
                            ttft_ms=ttft_ms,
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


async def multiplex(
    session_id: str,
    turn_id: str,
    lanes: list[tuple[str, ChatMessage]],
) -> AsyncIterator[str]:
    """Run N lanes concurrently, multiplexing their SSE events over one stream."""
    hub = _RunHub(session_id, turn_id)
    _hubs[(session_id, turn_id)] = hub
    # Values identical for every lane (tool credentials, extracted documents, image
    # provider) are resolved by the first lane and reused by the rest.
    shared: dict = {}

    async def _wrap(lane_id: str, msg: ChatMessage) -> None:
        await run_lane(session_id, lane_id, turn_id, msg, hub, shared)

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
