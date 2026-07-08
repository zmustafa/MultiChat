import React, { useMemo, useState } from "react";

interface Cell {
  node: React.ReactNode;
  text: string;
}

/** Flatten any React node subtree into its plain text (used as sort/filter keys). */
function nodeText(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (React.isValidElement(node)) {
    return nodeText((node.props as { children?: React.ReactNode }).children);
  }
  return "";
}

/** Direct element children of `node` whose tag is one of `types`. */
function childElements(
  node: React.ReactNode,
  types: string[]
): React.ReactElement[] {
  return React.Children.toArray(node).filter(
    (c): c is React.ReactElement =>
      React.isValidElement(c) &&
      typeof c.type === "string" &&
      types.includes(c.type)
  );
}

function cellsFrom(node: React.ReactNode): Cell[] {
  return childElements(node, ["th", "td"]).map((el) => {
    const children = (el.props as { children?: React.ReactNode }).children;
    return { node: children, text: nodeText(children).trim() };
  });
}

/** Render a table (header + body plain-text) to a crisp PNG blob via canvas. */
async function renderTablePng(
  headerTexts: string[],
  rowTexts: string[][]
): Promise<Blob> {
  const scale = 2;
  const font = "13px -apple-system, 'Segoe UI', Roboto, sans-serif";
  const headFont = "600 13px -apple-system, 'Segoe UI', Roboto, sans-serif";
  const hpad = 12;
  const vpad = 8;
  const lineH = 18;
  const maxCol = 340;
  const minCol = 40;
  const ncol = headerTexts.length;
  const measure = document.createElement("canvas").getContext("2d")!;

  const wrap = (text: string, f: string, maxW: number): string[] => {
    measure.font = f;
    const words = text.split(/\s+/).filter(Boolean);
    const lines: string[] = [];
    let cur = "";
    for (const w of words) {
      const test = cur ? cur + " " + w : w;
      if (measure.measureText(test).width > maxW && cur) {
        lines.push(cur);
        cur = w;
      } else {
        cur = test;
      }
    }
    if (cur) lines.push(cur);
    return lines.length ? lines : [""];
  };

  const colW: number[] = [];
  for (let c = 0; c < ncol; c++) {
    measure.font = headFont;
    let w = measure.measureText(headerTexts[c] || "").width;
    measure.font = font;
    for (const row of rowTexts) w = Math.max(w, measure.measureText(row[c] || "").width);
    colW[c] = Math.max(minCol, Math.min(maxCol, Math.ceil(w)));
  }

  const headLines = headerTexts.map((t, c) => wrap(t || "", headFont, colW[c]));
  const headH = Math.max(1, ...headLines.map((l) => l.length)) * lineH + 2 * vpad;
  const bodyLines = rowTexts.map((row) =>
    row.map((t, c) => wrap(t || "", font, colW[c]))
  );
  const rowH = bodyLines.map(
    (cells) => Math.max(1, ...cells.map((l) => l.length)) * lineH + 2 * vpad
  );

  const totalW = colW.reduce((a, b) => a + b + 2 * hpad, 0) + 1;
  const totalH = headH + rowH.reduce((a, b) => a + b, 0) + 1;

  const canvas = document.createElement("canvas");
  canvas.width = Math.ceil(totalW * scale);
  canvas.height = Math.ceil(totalH * scale);
  const ctx = canvas.getContext("2d")!;
  ctx.scale(scale, scale);
  ctx.textBaseline = "top";

  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, totalW, totalH);
  ctx.fillStyle = "#f3f4f6";
  ctx.fillRect(0, 0, totalW, headH);

  const drawRow = (lines: string[][], y: number, f: string, color: string) => {
    ctx.font = f;
    ctx.fillStyle = color;
    let x = 0;
    for (let c = 0; c < ncol; c++) {
      let ty = y + vpad;
      for (const ln of lines[c]) {
        ctx.fillText(ln, x + hpad, ty);
        ty += lineH;
      }
      x += colW[c] + 2 * hpad;
    }
  };

  drawRow(headLines, 0, headFont, "#111827");
  let y = headH;
  bodyLines.forEach((lines, i) => {
    ctx.fillStyle = i % 2 ? "#f9fafb" : "#ffffff";
    ctx.fillRect(0, y, totalW, rowH[i]);
    drawRow(lines, y, font, "#374151");
    y += rowH[i];
  });

  ctx.strokeStyle = "#e5e7eb";
  ctx.lineWidth = 1;
  ctx.beginPath();
  let x = 0.5;
  for (let c = 0; c <= ncol; c++) {
    ctx.moveTo(x, 0);
    ctx.lineTo(x, totalH);
    if (c < ncol) x += colW[c] + 2 * hpad;
  }
  ctx.moveTo(0, 0.5);
  ctx.lineTo(totalW, 0.5);
  let yy = headH;
  ctx.moveTo(0, yy + 0.5);
  ctx.lineTo(totalW, yy + 0.5);
  for (const h of rowH) {
    yy += h;
    ctx.moveTo(0, yy + 0.5);
    ctx.lineTo(totalW, yy + 0.5);
  }
  ctx.stroke();

  return await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("toBlob failed"))),
      "image/png"
    )
  );
}

/**
 * A GFM markdown table rendered as a sortable + filterable, nicely styled table.
 * Cell content keeps its markdown formatting (bold, code, links); sorting/filtering
 * uses the plain-text projection of each cell.
 */
export function MarkdownTable({ children }: { children?: React.ReactNode }) {
  const { headers, rows } = useMemo(() => {
    const top = childElements(children, ["thead", "tbody"]);
    const thead = top.find((c) => c.type === "thead");
    const tbody = top.find((c) => c.type === "tbody");
    const headTr = thead
      ? childElements((thead.props as { children?: React.ReactNode }).children, ["tr"])[0]
      : undefined;
    const headers: Cell[] = headTr
      ? cellsFrom((headTr.props as { children?: React.ReactNode }).children)
      : [];
    const rows: Cell[][] = tbody
      ? childElements((tbody.props as { children?: React.ReactNode }).children, ["tr"]).map(
          (tr) => cellsFrom((tr.props as { children?: React.ReactNode }).children)
        )
      : [];
    return { headers, rows };
  }, [children]);

  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);
  const [copied, setCopied] = useState<"md" | "table" | "image" | null>(null);

  const view = useMemo(() => {
    let r = rows;
    const q = filter.trim().toLowerCase();
    if (q) r = r.filter((row) => row.some((c) => c.text.toLowerCase().includes(q)));
    if (sort) {
      const { col, dir } = sort;
      r = [...r].sort((a, b) => {
        const av = a[col]?.text ?? "";
        const bv = b[col]?.text ?? "";
        const an = parseFloat(av.replace(/[^0-9.\-]/g, ""));
        const bn = parseFloat(bv.replace(/[^0-9.\-]/g, ""));
        const numeric =
          av.trim() !== "" &&
          bv.trim() !== "" &&
          !Number.isNaN(an) &&
          !Number.isNaN(bn);
        const cmp = numeric ? an - bn : av.localeCompare(bv);
        return cmp * dir;
      });
    }
    return r;
  }, [rows, filter, sort]);

  // If parsing failed (unexpected structure), fall back to a plain styled table.
  if (headers.length === 0) {
    return <table className="markdown-fallback-table">{children}</table>;
  }

  function toggleSort(col: number) {
    setSort((prev) =>
      prev && prev.col === col
        ? prev.dir === 1
          ? { col, dir: -1 }
          : null
        : { col, dir: 1 }
    );
  }

  function flash(kind: "md" | "table" | "image") {
    setCopied(kind);
    window.setTimeout(() => setCopied((c) => (c === kind ? null : c)), 1200);
  }

  /** Copy the current (sorted/filtered) view as a GFM markdown table. */
  async function copyMarkdown() {
    const esc = (t: string) => t.replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
    const head = `| ${headers.map((h) => esc(h.text)).join(" | ")} |`;
    const sep = `| ${headers.map(() => "---").join(" | ")} |`;
    const body = view
      .map((r) => `| ${headers.map((_, i) => esc(r[i]?.text ?? "")).join(" | ")} |`)
      .join("\n");
    try {
      await navigator.clipboard.writeText([head, sep, body].join("\n"));
      flash("md");
    } catch {
      /* clipboard unavailable */
    }
  }

  /** Copy as a rich table (HTML + TSV) so it pastes into Sheets/Excel/Docs. */
  async function copyAsTable() {
    const escHtml = (t: string) =>
      t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const html =
      "<table><thead><tr>" +
      headers.map((h) => `<th>${escHtml(h.text)}</th>`).join("") +
      "</tr></thead><tbody>" +
      view
        .map(
          (r) =>
            "<tr>" +
            headers.map((_, i) => `<td>${escHtml(r[i]?.text ?? "")}</td>`).join("") +
            "</tr>"
        )
        .join("") +
      "</tbody></table>";
    const tsv = [
      headers.map((h) => h.text).join("\t"),
      ...view.map((r) => headers.map((_, i) => r[i]?.text ?? "").join("\t")),
    ].join("\n");
    try {
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/html": new Blob([html], { type: "text/html" }),
            "text/plain": new Blob([tsv], { type: "text/plain" }),
          }),
        ]);
      } else {
        await navigator.clipboard.writeText(tsv);
      }
      flash("table");
    } catch {
      try {
        await navigator.clipboard.writeText(tsv);
        flash("table");
      } catch {
        /* clipboard unavailable */
      }
    }
  }

  /** Render the current view to a PNG and copy it (falls back to download). */
  async function copyImage() {
    try {
      const blob = await renderTablePng(
        headers.map((h) => h.text),
        view.map((r) => headers.map((_, i) => r[i]?.text ?? ""))
      );
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
        await navigator.clipboard.write([
          new ClipboardItem({ "image/png": blob }),
        ]);
      } else {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "table.png";
        a.click();
        URL.revokeObjectURL(url);
      }
      flash("image");
    } catch {
      /* rendering/clipboard unavailable */
    }
  }

  const copyBtns = (
    <div className="ml-auto flex items-center gap-1">
      <button
        onClick={copyMarkdown}
        title="Copy as markdown table"
        className="rounded border border-gray-300 bg-white px-1.5 py-0.5 text-[10px] font-medium text-gray-600 transition hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        {copied === "md" ? "✓ copied" : "⧉ Markdown"}
      </button>
      <button
        onClick={copyAsTable}
        title="Copy as table (paste into Sheets, Excel, Docs…)"
        className="rounded border border-gray-300 bg-white px-1.5 py-0.5 text-[10px] font-medium text-gray-600 transition hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        {copied === "table" ? "✓ copied" : "⊞ Table"}
      </button>
      <button
        onClick={copyImage}
        title="Copy as image (falls back to download)"
        className="rounded border border-gray-300 bg-white px-1.5 py-0.5 text-[10px] font-medium text-gray-600 transition hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        {copied === "image" ? "✓ copied" : "🖼 Image"}
      </button>
    </div>
  );

  return (
    <div className="my-3 overflow-hidden rounded-lg border border-gray-200 shadow-sm dark:border-gray-700">
      <div className="flex items-center gap-2 border-b border-gray-100 bg-gray-50 px-2 py-1 dark:border-gray-800 dark:bg-gray-800/40">
        {rows.length > 3 && (
          <>
            <span className="text-gray-400">🔎</span>
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter rows…"
              className="w-40 rounded border border-gray-300 bg-white px-2 py-0.5 text-xs outline-none focus:border-brand dark:border-gray-600 dark:bg-gray-900"
            />
            <span className="text-[10px] text-gray-400">
              {view.length}/{rows.length}
            </span>
            {(sort || filter) && (
              <button
                onClick={() => {
                  setSort(null);
                  setFilter("");
                }}
                className="text-[10px] text-blue-500 hover:underline"
              >
                reset
              </button>
            )}
          </>
        )}
        {copyBtns}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="bg-gray-100 dark:bg-gray-800/60">
              {headers.map((h, i) => (
                <th
                  key={i}
                  onClick={() => toggleSort(i)}
                  title="Click to sort"
                  className="cursor-pointer select-none whitespace-nowrap border-b border-gray-200 px-3 py-2 text-left font-semibold text-gray-700 transition hover:bg-gray-200 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-700/60"
                >
                  <span className="inline-flex items-center gap-1">
                    {h.node}
                    <span className="text-[10px] text-gray-400">
                      {sort?.col === i ? (sort.dir === 1 ? "▲" : "▼") : "⇅"}
                    </span>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.map((row, ri) => (
              <tr
                key={ri}
                className="border-b border-gray-100 transition last:border-0 odd:bg-white even:bg-gray-50/60 hover:bg-brand/5 dark:border-gray-800 dark:odd:bg-gray-900 dark:even:bg-gray-800/30 dark:hover:bg-brand/10"
              >
                {row.map((c, ci) => (
                  <td
                    key={ci}
                    className="px-3 py-1.5 align-top text-gray-700 dark:text-gray-200"
                  >
                    {c.node}
                  </td>
                ))}
              </tr>
            ))}
            {view.length === 0 && (
              <tr>
                <td
                  colSpan={headers.length}
                  className="px-3 py-3 text-center text-gray-400"
                >
                  No matching rows.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
