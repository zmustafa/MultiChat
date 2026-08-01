import { apiFetch, mediaUrl } from "../api/client";
import { svgToPngDataUrl } from "../components/Mermaid";

export interface ExportDiagram {
  code: string;
  image: string;
  width: number;
  height: number;
}

/**
 * Rasterize every mermaid diagram rendered inside `container`.
 *
 * The backend cannot draw mermaid (that needs a browser), so the chat UI hands over the
 * diagrams it is already showing — keyed by their source — and the PDF embeds those exact
 * images. A diagram that fails to rasterize is simply omitted and falls back to its source
 * text in the document.
 */
export async function collectDiagrams(
  container: HTMLElement | null,
): Promise<ExportDiagram[]> {
  if (!container) return [];
  const nodes = Array.from(
    container.querySelectorAll<HTMLElement>("[data-mermaid-code]"),
  );
  const diagrams: ExportDiagram[] = [];
  for (const node of nodes) {
    const svg = node.querySelector("svg");
    if (!svg) continue;
    try {
      const { image, width, height } = await svgToPngDataUrl(svg as SVGSVGElement);
      diagrams.push({ code: node.dataset.mermaidCode || "", image, width, height });
    } catch {
      /* keep exporting — this diagram will appear as its mermaid source instead */
    }
  }
  return diagrams;
}

/**
 * Export ONE assistant response as a US Letter PDF and download it.
 *
 * `container` is the DOM node holding that message's rendered markdown; its diagrams are
 * captured so the PDF matches what the lane shows.
 */
export async function downloadMessagePdf(
  sessionId: string,
  messageId: string,
  container: HTMLElement | null,
): Promise<void> {
  const diagrams = await collectDiagrams(container);
  const res = await apiFetch<{ url: string; download_name: string }>(
    `/api/sessions/${sessionId}/messages/${messageId}/export`,
    { method: "POST", body: JSON.stringify({ diagrams }) },
  );
  const a = document.createElement("a");
  a.href = mediaUrl(res.url);
  a.download = res.download_name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
