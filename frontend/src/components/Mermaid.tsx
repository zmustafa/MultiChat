import { useCallback, useEffect, useRef, useState } from "react";
import type MermaidApi from "mermaid";

// Lazy-load the (heavy) mermaid library on first diagram render so it doesn't bloat the
// main bundle / first paint. Cached after the first import.
let _mermaidPromise: Promise<typeof MermaidApi> | null = null;
function loadMermaid(): Promise<typeof MermaidApi> {
  if (!_mermaidPromise) {
    _mermaidPromise = import("mermaid").then((m) => m.default);
  }
  return _mermaidPromise;
}

function initMermaid(mermaid: typeof MermaidApi, dark: boolean) {
  mermaid.initialize({
    startOnLoad: false,
    theme: dark ? "dark" : "default",
    securityLevel: "strict",
    // Prevent mermaid from injecting its "Syntax error" bomb SVG into the DOM when a
    // (possibly still-streaming / incomplete) diagram fails to parse or render.
    suppressErrorRendering: true,
    // Render labels as native SVG <text> instead of HTML <foreignObject>. This keeps the
    // diagram fully rasterizable so "Copy image" (SVG → PNG on a canvas) works without the
    // foreignObject canvas-tainting that would otherwise block clipboard export.
    flowchart: { htmlLabels: false },
    class: { htmlLabels: false },
  });
}

/** Track the app's light/dark mode by observing the <html> `dark` class. */
function useIsDark(): boolean {
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark")
  );
  useEffect(() => {
    const el = document.documentElement;
    const obs = new MutationObserver(() =>
      setDark(el.classList.contains("dark"))
    );
    obs.observe(el, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);
  return dark;
}

let counter = 0;

/**
 * Repair the most common LLM-generated mermaid mistakes that break parsing. Conservative:
 * only rewrites clearly-malformed lines, and the caller re-validates with mermaid.parse
 * before actually rendering the result — so a valid diagram is never altered.
 */
function sanitizeMermaid(code: string): string {
  return code
    .split("\n")
    .map((line) => {
      // Stray closing bracket after a quoted subgraph title, e.g.
      //   subgraph "Application-Subnet"]   →   subgraph "Application-Subnet"
      // Valid forms are `subgraph "Title"` or `subgraph id["Title"]`; models sometimes
      // mix them and emit a quoted title WITH a trailing `]` (no opening `[`), which
      // fails the whole diagram.
      if (/^\s*subgraph\b/.test(line) && line.includes("]") && !line.includes("[")) {
        return line.replace(/\]\s*$/, "");
      }
      return line;
    })
    .join("\n");
}

// Cache rendered SVGs by (theme + code). Mermaid layout is expensive and diagrams re-mount
// often (a lane re-renders whenever another lane streams), so without this the same diagram
// is re-laid-out repeatedly. Keyed on the full code string, so partial/streaming code never
// collides with the finished diagram.
const _svgCache = new Map<string, string>();

/** Remove any orphaned mermaid error/temporary nodes appended directly to <body>. */
function sweepOrphans(id: string) {
  document.getElementById("d" + id)?.remove();
  const orphan = document.getElementById(id);
  if (orphan && orphan.parentElement === document.body) orphan.remove();
  // Defensive: mermaid can leave stray error graphics attached to <body>.
  document
    .querySelectorAll('body > svg[id^="mermaid-"], body > svg[aria-roledescription="error"]')
    .forEach((el) => el.remove());
}

/** Rasterize a rendered mermaid <svg> into a PNG blob via an off-screen canvas. */
function svgToPngBlob(svgEl: SVGSVGElement, scale?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    // Use the SVG's intrinsic (viewBox) size, NOT its on-screen rect. Mermaid renders the
    // <svg> at width="100%", so it's shrunk to fit the lane column — rasterizing that small
    // displayed size yields a tiny, blurry PNG. The viewBox gives the diagram's true size.
    const vb = svgEl.viewBox?.baseVal;
    const rect = svgEl.getBoundingClientRect();
    const width =
      (vb && vb.width) ||
      rect.width ||
      parseFloat(svgEl.getAttribute("width") || "") ||
      800;
    const height =
      (vb && vb.height) ||
      rect.height ||
      parseFloat(svgEl.getAttribute("height") || "") ||
      600;
    // Upscale so text stays crisp — small diagrams get a bigger multiplier — while capping
    // the output so the canvas never gets absurdly large.
    const s =
      scale ??
      Math.max(2, Math.min(4, 2000 / width, 6000 / width, 6000 / height));

    const clone = svgEl.cloneNode(true) as SVGSVGElement;
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", String(width));
    clone.setAttribute("height", String(height));
    clone.setAttribute("viewBox", `0 0 ${width} ${height}`);
    // Loading an SVG that contains <foreignObject> into an <img> taints the canvas
    // (a browser security rule), which blocks PNG export. Flatten any HTML labels into
    // native SVG <text> on the clone so the diagram rasterizes cleanly.
    flattenForeignObjects(clone, svgEl);

    const data = new XMLSerializer().serializeToString(clone);
    const url = URL.createObjectURL(
      new Blob([data], { type: "image/svg+xml;charset=utf-8" }),
    );
    const img = new Image();
    img.onload = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.ceil(width * s));
        canvas.height = Math.max(1, Math.ceil(height * s));
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("no 2d context");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.scale(s, s);
        ctx.drawImage(img, 0, 0, width, height);
        URL.revokeObjectURL(url);
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error("toBlob failed"))),
          "image/png",
        );
      } catch (err) {
        URL.revokeObjectURL(url);
        reject(err);
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("svg image load failed"));
    };
    img.src = url;
  });
}

/** Replace <foreignObject> HTML labels with native SVG <text> so the SVG can be
 *  rasterized to a canvas without tainting it. Uses the live element to read the
 *  rendered text color/size for a faithful copy. */
function flattenForeignObjects(clone: SVGSVGElement, live: SVGSVGElement) {
  const cloneFos = Array.from(clone.querySelectorAll("foreignObject"));
  const liveFos = Array.from(live.querySelectorAll("foreignObject"));
  cloneFos.forEach((fo, i) => {
    const w = parseFloat(fo.getAttribute("width") || "0");
    const h = parseFloat(fo.getAttribute("height") || "0");
    const x = parseFloat(fo.getAttribute("x") || "0");
    const y = parseFloat(fo.getAttribute("y") || "0");
    const liveSpan = liveFos[i]?.querySelector("span, div, p") as HTMLElement | null;
    const style = liveSpan ? getComputedStyle(liveSpan) : null;
    const lines = (fo.textContent || "").split("\n").map((s) => s.trim()).filter(Boolean);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", String(x + w / 2));
    text.setAttribute("y", String(y + h / 2));
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "central");
    text.setAttribute("font-family", style?.fontFamily || "sans-serif");
    text.setAttribute("font-size", style?.fontSize || "14px");
    text.setAttribute("fill", style?.color || "#1f2937");
    const lineHeight = parseFloat(style?.fontSize || "14") * 1.2;
    lines.forEach((line, li) => {
      const tspan = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
      tspan.setAttribute("x", String(x + w / 2));
      tspan.setAttribute(
        "dy",
        li === 0 ? String(-((lines.length - 1) * lineHeight) / 2) : String(lineHeight),
      );
      tspan.textContent = line;
      text.appendChild(tspan);
    });
    fo.parentNode?.replaceChild(text, fo);
  });
}

export function Mermaid({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const dark = useIsDark();
  const [svg, setSvg] = useState<string>("");
  const [rendered, setRendered] = useState(false);
  const [copyState, setCopyState] = useState<"" | "ok" | "svg" | "err">("");
  const [expanded, setExpanded] = useState(false);
  const firstRun = useRef(true);

  useEffect(() => {
    let cancelled = false;
    // Instant path: this exact diagram (same theme) was already rendered — reuse it.
    const cacheKey = (dark ? "d|" : "l|") + code;
    const cached = _svgCache.get(cacheKey);
    if (cached) {
      setSvg(cached);
      setRendered(true);
      firstRun.current = false;
      return;
    }
    const id = `mermaid-${counter++}`;
    // Debounce rendering. While a diagram is still streaming in, `code` changes on every
    // chunk; re-rendering mermaid each time makes the diagram's width/height oscillate,
    // which flickers the whole layout horizontally. Wait for the code to stop changing
    // (streaming paused/finished) before (re)rendering, and keep showing the last good
    // diagram on transient parse failures instead of flipping back to raw text.
    // The FIRST render (mount) is immediate though — a complete diagram that (re)mounts
    // (e.g. this lane re-rendered while another lane streams) must not show the black
    // raw-code fallback for the debounce window before it paints.
    const delay = firstRun.current ? 0 : 300;
    firstRun.current = false;
    const timer = setTimeout(() => {
      (async () => {
        try {
          const mermaid = await loadMermaid();
          if (cancelled) return;
          initMermaid(mermaid, dark);
          // Validate first (suppressErrors => returns false instead of throwing).
          let src = code;
          let ok = !!(await mermaid.parse(code, { suppressErrors: true }));
          if (!ok) {
            // Try to repair common LLM syntax mistakes, then re-validate. Only render the
            // repaired version if it actually parses.
            const fixed = sanitizeMermaid(code);
            if (
              fixed !== code &&
              !!(await mermaid.parse(fixed, { suppressErrors: true }))
            ) {
              src = fixed;
              ok = true;
            }
          }
          if (!ok) {
            // Keep showing whatever we already have (last good diagram, or the raw-code
            // fallback if nothing has rendered yet) rather than flickering on partial input.
            return;
          }
          const res = await mermaid.render(id, src);
          if (!cancelled) {
            if (_svgCache.size > 200) _svgCache.clear();
            _svgCache.set(cacheKey, res.svg);
            setSvg(res.svg);
            setRendered(true);
          }
        } catch {
          // Keep the previous state — do not flip to the raw-code view mid-stream.
        } finally {
          sweepOrphans(id);
        }
      })();
    }, delay);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code, dark]);

  async function copyImage() {
    const svgEl = ref.current?.querySelector("svg");
    if (!svgEl) return;
    try {
      const blob = await svgToPngBlob(svgEl as SVGSVGElement);
      await navigator.clipboard.write([
        new ClipboardItem({ "image/png": blob }),
      ]);
      setCopyState("ok");
    } catch {
      // Fallback: copy the SVG markup as text if PNG/clipboard-image is unavailable.
      try {
        await navigator.clipboard.writeText(
          new XMLSerializer().serializeToString(svgEl),
        );
        setCopyState("svg");
      } catch {
        setCopyState("err");
      }
    }
    setTimeout(() => setCopyState(""), 1800);
  }

  // While the diagram is incomplete (e.g. still streaming) or invalid, show the
  // raw definition as a neutral code block rather than a scary error graphic.
  if (!rendered) {
    return (
      <pre className="my-2 overflow-x-auto rounded bg-gray-100 p-2 text-xs dark:bg-gray-800">
        <code>{code}</code>
      </pre>
    );
  }

  const copyLabel =
    copyState === "ok"
      ? "Copied ✓"
      : copyState === "svg"
        ? "Copied SVG ✓"
        : copyState === "err"
          ? "Copy failed"
          : "Copy image";

  return (
    <>
      <div className="group relative my-2 flex justify-center overflow-x-auto rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="absolute right-1 top-1 z-10 flex gap-1 opacity-0 transition group-hover:opacity-100">
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded border border-gray-300 bg-white/90 px-2 py-0.5 text-[11px] text-gray-600 shadow-sm transition hover:bg-white hover:text-gray-900 dark:border-gray-600 dark:bg-gray-800/90 dark:text-gray-300 dark:hover:bg-gray-800"
            title="Expand diagram (zoom & pan)"
          >
            ⤢ Expand
          </button>
          <button
            type="button"
            onClick={copyImage}
            className="rounded border border-gray-300 bg-white/90 px-2 py-0.5 text-[11px] text-gray-600 shadow-sm transition hover:bg-white hover:text-gray-900 dark:border-gray-600 dark:bg-gray-800/90 dark:text-gray-300 dark:hover:bg-gray-800"
            title="Copy diagram as an image"
          >
            {copyLabel}
          </button>
        </div>
        <div
          ref={ref}
          onClick={() => setExpanded(true)}
          className="max-w-full cursor-zoom-in [&_svg]:!max-w-full [&_svg]:h-auto"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
      {expanded && <DiagramModal svg={svg} onClose={() => setExpanded(false)} />}
    </>
  );
}

/** Full-screen diagram viewer with zoom (wheel / buttons) + pan (drag) + fit/reset. */
function DiagramModal({ svg, onClose }: { svg: string; onClose: () => void }) {
  const [scale, setScale] = useState(1);
  const [tx, setTx] = useState(0);
  const [ty, setTy] = useState(0);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const viewportRef = useRef<HTMLDivElement>(null);

  // The diagram's natural size comes from the SVG viewBox; mermaid SVGs use width="100%"
  // so they'd otherwise collapse/measure as 0 in the auto-sized modal box.
  const dims = (() => {
    const m = /viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"/.exec(svg);
    return m ? { w: parseFloat(m[1]), h: parseFloat(m[2]) } : { w: 800, h: 600 };
  })();

  // Scale the diagram to fill most of the viewport (mermaid diagrams render at their small
  // intrinsic size, so opening at 100% would look no bigger than the inline copy).
  const fit = useCallback(() => {
    const vp = viewportRef.current;
    if (!vp || !dims.w || !dims.h) return;
    const next = Math.min(
      8,
      Math.max(0.2, Math.min((vp.clientWidth * 0.92) / dims.w, (vp.clientHeight * 0.92) / dims.h)),
    );
    setScale(next);
    setTx(0);
    setTy(0);
  }, [dims.w, dims.h]);

  // Fit once the SVG has laid out.
  useEffect(() => {
    const raf = requestAnimationFrame(fit);
    return () => cancelAnimationFrame(raf);
  }, [fit, svg]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "+" || e.key === "=") setScale((s) => Math.min(8, s * 1.2));
      if (e.key === "-") setScale((s) => Math.max(0.2, s / 1.2));
      if (e.key === "0") fit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, fit]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-white"
        onClick={(e) => e.stopPropagation()}
      >
        <span className="text-sm font-medium">Diagram viewer</span>
        <div className="flex items-center gap-1 text-sm">
          <button
            onClick={() => setScale((s) => Math.max(0.2, s / 1.2))}
            className="rounded px-2 py-0.5 hover:bg-white/10"
            title="Zoom out (-)"
          >
            −
          </button>
          <span className="w-12 text-center text-xs tabular-nums">
            {Math.round(scale * 100)}%
          </span>
          <button
            onClick={() => setScale((s) => Math.min(8, s * 1.2))}
            className="rounded px-2 py-0.5 hover:bg-white/10"
            title="Zoom in (+)"
          >
            +
          </button>
          <button
            onClick={fit}
            className="ml-1 rounded px-2 py-0.5 text-xs hover:bg-white/10"
            title="Fit to screen (0)"
          >
            Fit
          </button>
          <button
            onClick={onClose}
            className="ml-1 rounded px-2 py-0.5 hover:bg-white/10"
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>
      </div>
      <div
        className="relative flex-1 overflow-hidden"
        ref={viewportRef}
        onClick={(e) => e.stopPropagation()}
        onWheel={(e) => {
          const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
          setScale((s) => Math.min(8, Math.max(0.2, s * factor)));
        }}
        onMouseDown={(e) => {
          drag.current = { x: e.clientX, y: e.clientY, tx, ty };
        }}
        onMouseMove={(e) => {
          if (!drag.current) return;
          setTx(drag.current.tx + (e.clientX - drag.current.x));
          setTy(drag.current.ty + (e.clientY - drag.current.y));
        }}
        onMouseUp={() => (drag.current = null)}
        onMouseLeave={() => (drag.current = null)}
        style={{ cursor: drag.current ? "grabbing" : "grab" }}
      >
        <div
          className="absolute left-1/2 top-1/2 rounded-lg bg-white p-4 shadow-2xl dark:bg-gray-900 [&_svg]:!h-full [&_svg]:!w-full [&_svg]:max-w-none"
          style={{
            width: dims.w,
            height: dims.h,
            transform: `translate(-50%, -50%) translate(${tx}px, ${ty}px) scale(${scale})`,
            transformOrigin: "center center",
          }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>
  );
}
