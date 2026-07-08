/** Small helpers for detecting the kinds of content inside an assistant message. */

export interface ContentBadge {
  icon: string;
  label: string;
}

/** Badges describing what a response contains (diagram / code / table). */
export function contentBadges(content: string): ContentBadge[] {
  const badges: ContentBadge[] = [];
  if (/```\s*mermaid/i.test(content)) badges.push({ icon: "📊", label: "diagram" });
  const fences = content.match(/```(\w*)/g) || [];
  if (fences.some((f) => !/mermaid/i.test(f)))
    badges.push({ icon: "❮❯", label: "code" });
  if (/^\s*\|.*\|\s*$/m.test(content) && /\|\s*:?-{2,}/.test(content))
    badges.push({ icon: "▦", label: "table" });
  return badges;
}

/**
 * Detect whether a plain (non-language) code block actually holds delimited tabular
 * data (CSV / TSV / pipe). Returns a GFM markdown table string that can be rendered
 * as an interactive table, or null if it doesn't look tabular.
 */
export function delimitedToMarkdown(text: string): string | null {
  const rawLines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (rawLines.length < 2) return null;
  // Skip content that is clearly source code rather than data.
  if (/[{};]|=>|\bfunction\b|\bconst\b|\bimport\b|<\/?\w+>/.test(text)) return null;

  for (const delim of ["\t", "|", ","]) {
    const lines = rawLines.filter((l) => !/^[\s|:+-]+$/.test(l)); // drop separator rows
    const rows = lines.map((l) => {
      let cells = l.split(delim).map((c) => c.trim());
      if (delim === "|")
        cells = cells.filter(
          (c, i, a) => !((i === 0 || i === a.length - 1) && c === "")
        );
      return cells;
    });
    const cols = rows[0]?.length || 0;
    if (cols < 2 || rows.length < 2) continue;
    const consistent = rows.filter((r) => r.length === cols).length;
    if (consistent < rows.length) continue; // require strict column alignment

    const esc = (c: string) => c.replace(/\|/g, "\\|");
    const header = rows[0];
    const body = rows.slice(1);
    return [
      `| ${header.map(esc).join(" | ")} |`,
      `| ${header.map(() => "---").join(" | ")} |`,
      ...body.map((r) => `| ${header.map((_, i) => esc(r[i] ?? "")).join(" | ")} |`),
    ].join("\n");
  }
  return null;
}
