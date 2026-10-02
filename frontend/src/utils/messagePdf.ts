import { apiFetch, downloadMedia } from "../api/client";
import { useEffect, useState } from "react";
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

export type MessageExportFormat = "pdf" | "docx";

/**
 * Export ONE assistant response as a US Letter PDF or a Word document and download it.
 *
 * `container` is the DOM node holding that message's rendered markdown; its diagrams are
 * captured so the document matches what the lane shows.
 */
export async function downloadMessage(
  sessionId: string,
  messageId: string,
  container: HTMLElement | null,
  fmt: MessageExportFormat = "pdf",
  includePrompt = true,
): Promise<void> {
  const diagrams = await collectDiagrams(container);
  const res = await apiFetch<{ url: string; download_name: string }>(
    `/api/sessions/${sessionId}/messages/${messageId}/export?fmt=${fmt}`,
    {
      method: "POST",
      body: JSON.stringify({ diagrams, include_prompt: includePrompt }),
    },
  );
  await downloadMedia(res.url, res.download_name);
}

export function downloadMessagePdf(
  sessionId: string,
  messageId: string,
  container: HTMLElement | null,
  includePrompt = true,
): Promise<void> {
  return downloadMessage(sessionId, messageId, container, "pdf", includePrompt);
}

export function downloadMessageDocx(
  sessionId: string,
  messageId: string,
  container: HTMLElement | null,
  includePrompt = true,
): Promise<void> {
  return downloadMessage(sessionId, messageId, container, "docx", includePrompt);
}

const INCLUDE_PROMPT_KEY = "multichat.export.includePrompt";
const INCLUDE_PROMPT_EVENT = "multichat:export-include-prompt";

export function getExportIncludePrompt(): boolean {
  try {
    return localStorage.getItem(INCLUDE_PROMPT_KEY) !== "false";
  } catch {
    return true;
  }
}

export function setExportIncludePrompt(value: boolean): void {
  try {
    localStorage.setItem(INCLUDE_PROMPT_KEY, String(value));
  } catch {
    /* storage unavailable — the choice just won't persist */
  }
  window.dispatchEvent(new CustomEvent(INCLUDE_PROMPT_EVENT, { detail: value }));
}

/** Shared "include the request message" export preference, kept in sync across every response. */
export function useExportIncludePrompt(): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(getExportIncludePrompt);
  useEffect(() => {
    const sync = () => setValue(getExportIncludePrompt());
    window.addEventListener(INCLUDE_PROMPT_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(INCLUDE_PROMPT_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return [value, setExportIncludePrompt];
}
