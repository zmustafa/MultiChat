"""System-prompt guidance injected into every lane run.

This is prose, not logic: keeping several hundred lines of prompt text inline in the
streaming module made the agent loop hard to read and made prompt edits look like changes
to the runtime. The injection helpers live here with the text they inject.
"""
from __future__ import annotations

TOOL_GUIDANCE_TEMPLATE = (
    "You have tools that produce REAL, downloadable files: {tools}. "
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

# Weaker models weight recency, so the rule is repeated on the latest user message.
TOOL_REMINDER = (
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

DIAGRAM_GUIDANCE = (
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


def prepend_system(messages: list, guidance: str) -> None:
    """Fold `guidance` into the leading system message, creating one if absent."""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        base = messages[0].get("content") or ""
        sep = "\n\n" if base else ""
        messages[0] = {**messages[0], "content": f"{base}{sep}{guidance}"}
    else:
        messages.insert(0, {"role": "system", "content": guidance})


def append_to_last_user(messages: list, text: str) -> None:
    """Append `text` to the most recent user message, surviving multimodal content."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                messages[i] = {**m, "content": content + text}
            elif isinstance(content, list):
                messages[i] = {
                    **m,
                    "content": content + [{"type": "text", "text": text}],
                }
            break


def inject_tool_guidance(messages: list, tools: list) -> None:
    """Steer models to actually USE the file-generation tools instead of writing code.

    Some models (notably gemini) will output python-pptx/docx code and claim they
    "cannot create files" rather than calling generate_pptx/docx/xlsx/pdf. A firm
    system instruction fixes that across providers.
    """
    generators = sorted(
        t.definition.name for t in tools if t.definition.name.startswith("generate_")
    )
    if not generators:
        return
    prepend_system(messages, TOOL_GUIDANCE_TEMPLATE.format(tools=", ".join(generators)))
    append_to_last_user(messages, TOOL_REMINDER)


def inject_diagram_guidance(messages: list) -> None:
    """Tell models to draw diagrams as Mermaid, which the app renders as real visuals.

    Without this, models "add a diagram" by drawing ASCII-art boxes inside a plain code
    fence — which shows up as an unhelpful black text block. The frontend renders
    ```mermaid fenced blocks into actual SVG diagrams, so steer models there.
    """
    prepend_system(messages, DIAGRAM_GUIDANCE)
