# docs/assets

Media referenced by the main [README](../../README.md).

## Animated captures (`gif/`)

| File | Shows |
| --- | --- |
| `gif/hero.gif` | Hero: one prompt broadcast to four lanes, all streaming at once |
| `gif/compare-diff.gif` | Reading four lanes side-by-side, then the Diff view |
| `gif/judge.gif` | The Judge panel's synthesized best answer + export actions |
| `gif/focus-tools.gif` | A live tool call, then maximizing one lane to full width and restoring |
| `gif/usage.gif` | The Insights dashboard: usage counters, token cost, provider mix, activity trends |

Lanes used: `gpt-5.6-sol`, `claude-opus-5`, `claude-sonnet-5`, `gemini-3.6-flash` (all via the
GitHub Copilot provider). Captured at 2375×1275, auto-cropped to 1969×1064, encoded at
1250–1500 px wide.

## How these were captured

The four chat GIFs are recorded from the **chat screen only** — no settings or provider
screens (`usage.gif` is the exception: it is the Insights dashboard) — against a throwaway
database so no chat history is ever on screen:

1. Copy `backend/chatbot.db` to `backend/demo.db` and delete every row from `sessions`
   (`PRAGMA foreign_keys=ON` so lanes/turns cascade). Keep `providers`, `personas` and
   `tool_credentials` so the app still works. Leave `APP_ENCRYPTION_KEY` unset so the
   existing encrypted provider tokens still decrypt.
2. Run a second backend against it (`DATABASE_URL=sqlite:///.../demo.db`, its own
   `UPLOAD_DIR`) and a second Vite server pointed at that backend, so the everyday
   instance on :5001/:5000 is untouched.
3. Launch a chat from the **Cloud Solutions Architect** persona, set the four lanes,
   **collapse the session sidebar** (the `⏴` button — it persists via
   `localStorage["multichat_nav_collapsed"]`) and click **Fit**.
4. Use a **1900×1020** viewport and click **Fit** so the lanes span the full width. Do **not**
   use `document.documentElement.style.zoom` to scale the UI: the app root still sizes itself
   to the unzoomed viewport, so ~17% of every frame ends up as blank page background on the
   right.
5. Capture PNG frames at ~10 fps while the answer streams.

The encoder **auto-crops** to the union bounding box of real content across sampled frames,
which removes that dead space regardless of how the browser sizes its screenshot canvas. The
result is a **1.85:1** frame that is entirely UI. Verify with `ImageChops.difference` against a
solid background image — a healthy output has ≤ 3 px of uniform margin.

## The pointer

Playwright screenshots never contain the real mouse cursor, so the recordings inject a
synthetic one: a translucent blue disc (`rgba(37,99,235,0.32)` with a `rgba(59,130,246,0.95)`
ring and a white halo so it stays visible over dark code blocks), `position:fixed`,
`pointer-events:none`, at the top of the z-order. Register it with `page.addInitScript` so a
reload cannot lose it.

Before each interaction the pointer is eased to the target's `boundingBox()` centre over
~12–14 captured frames, then a ring pulses outward to mark the click. Page coordinates map
**1:1** to screenshot pixels here, so `boundingBox()` values can be used directly.

A useful side effect: the pointer's travel keeps consecutive frames visibly different, so far
fewer frames get merged as duplicates — the hero went from 37 surviving frames to 425, which
is why the motion reads as motion rather than as a slideshow.

> Every GIF is referenced **full width** in the README, not inside a two-column table. A GIF
> in a 50%-wide table cell renders at ~440 px on github.com no matter how large the file is,
> which was why an earlier pass looked tiny.

## Encoding

Three settings decide whether the result looks sharp or mushy:

- **`dither=Image.Dither.NONE`.** This UI is flat colour with small text. Floyd–Steinberg
  scatters pixels through the glyph anti-aliasing, which is exactly what makes GIF text look
  furry. Turning dithering off keeps letter edges hard *and* compresses better.
- **A generous palette (160–256 colours)** built **once for the whole animation**, from a
  stacked sheet of ~16 frames sampled across the run. Per-frame palettes make colours shift
  mid-run and defeat delta encoding.
- **`disposal=1`.** With a shared palette this lets Pillow store only the rectangle that
  changed per frame. `disposal=2` forces a full-size image every frame — the same hero frames
  came out at 9.8 MB with `disposal=2` versus 1.25 MB with `disposal=1`.

Near-identical frames are collapsed into extra **hold time** on the previous frame rather than
dropped, so real pauses in the run survive without the animation turning into a slideshow.

For scroll-heavy captures every pixel changes each frame, so delta encoding cannot help and
size grows linearly with frame count. Sample frames (`--step`) to hit a budget — unlike
lowering the resolution or enabling dither, dropping cadence costs no sharpness.

Budget: keep each GIF under ~5 MB so the README stays fast and stays inside GitHub's image
proxy limit.

Two capture traps worth knowing:

- **Capture something that actually moves.** Pillow merges consecutive identical frames, so a
  capture where nothing changes silently collapses to 2–3 frames. Check `Image.n_frames` on
  every output. Lanes are already scrolled to the bottom after streaming, so scrolling *down*
  does nothing — reset `scrollTop` to 0 first.
- **The Judge panel does not auto-scroll.** Its scroller is `.max-h-56.overflow-y-auto`; streamed
  text lands below the fold, so the visible region never changes. Scroll that element explicitly.
- **Scroll with `scrollTop`, not `mouse.wheel`.** Wheel events stall on the Diff view, and a
  stalled capture keeps writing frames into the folder while you move on to the next scene —
  which silently splices the wrong screen onto the end of the previous GIF.
- **Keep scroll ranges shallow.** Scrolling to the bottom of an answer lands in a wall of raw
  Bicep. The summary tables, tool-call chips and cited sources near the top are what make the
  feature legible.

Budget: keep each GIF under ~3 MB (hero under ~5 MB) so the README stays fast on mobile.

## Before publishing

Step through the frames and confirm no chat titles, folder names, account names, API keys
or uploaded filenames are readable in **any** frame.
