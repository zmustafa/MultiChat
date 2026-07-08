from __future__ import annotations

import httpx

from .base import ToolContext, ToolDef, ToolResult


class ImageGenerateTool:
    definition = ToolDef(
        name="generate_image",
        description="Generate an image from a text prompt. Returns a Markdown image the "
        "user can view. Use when the user asks to draw/create/generate an image.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What the image should depict"},
                "size": {
                    "type": "string",
                    "description": "e.g. 1024x1024, 1792x1024, 1024x1792",
                },
            },
            "required": ["prompt"],
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(content="No prompt provided.", citations=None)
        if not ctx.image_api_key:
            return ToolResult(
                content="Image generation isn't configured — add an OpenAI (or "
                "OpenAI-compatible) provider with an image model.",
                citations=None,
            )
        base = (ctx.image_base_url or "https://api.openai.com/v1").rstrip("/")
        model = ctx.image_model or "dall-e-3"
        size = args.get("size") or "1024x1024"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base}/images/generations",
                    headers={"Authorization": f"Bearer {ctx.image_api_key}"},
                    json={"model": model, "prompt": prompt, "size": size, "n": 1},
                )
            if resp.status_code >= 400:
                return ToolResult(
                    content=f"Image generation failed: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}",
                    citations=None,
                )
            data = resp.json()
            item = (data.get("data") or [{}])[0]
            url = item.get("url")
            if url:
                return ToolResult(content=f"![{prompt}]({url})", citations=None)
            b64 = item.get("b64_json")
            if b64:
                return ToolResult(
                    content=f"![{prompt}](data:image/png;base64,{b64})", citations=None
                )
            return ToolResult(content="No image returned.", citations=None)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Image generation error: {exc}", citations=None)
