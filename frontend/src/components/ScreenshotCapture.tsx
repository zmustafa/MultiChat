import { useCallback, useEffect, useRef, useState } from "react";

type CaptureState =
  | "requesting-permission"
  | "captured"
  | "selecting"
  | "cropped"
  | "sending"
  | "error";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Props {
  /** Called with the cropped PNG file once the user confirms. */
  onAttach: (file: File) => void | Promise<void>;
  /** Close the capture UI. */
  onClose: () => void;
}

const MIN_CROP = 10; // px in the source image

/**
 * User-approved screen-region screenshot capture.
 *
 * Flow: getDisplayMedia() → grab one frame → stop the stream → let the user drag a
 * rectangle over the still image → crop to a PNG blob → hand it to the chat composer.
 * The full screenshot is never sent; only the cropped region is attached.
 */
export function ScreenshotCapture({ onAttach, onClose }: Props) {
  const [state, setState] = useState<CaptureState>("requesting-permission");
  const [error, setError] = useState<string>("");
  const [imgUrl, setImgUrl] = useState<string>(""); // full-frame preview (object URL)
  const [sel, setSel] = useState<Rect | null>(null); // selection in displayed CSS px
  const [croppedUrl, setCroppedUrl] = useState<string>(""); // cropped preview

  // Full-resolution frame kept off-screen for accurate, high-DPI cropping.
  const frameCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);
  const croppedBlobRef = useRef<Blob | null>(null);

  const stopStream = useCallback((stream: MediaStream) => {
    stream.getTracks().forEach((t) => t.stop());
  }, []);

  const captureFrameFromStream = useCallback(
    async (stream: MediaStream) => {
      const video = document.createElement("video");
      video.srcObject = stream;
      video.muted = true;
      await video.play();
      // Wait until the first frame has real dimensions.
      await new Promise<void>((resolve) => {
        if (video.videoWidth) return resolve();
        video.onloadedmetadata = () => resolve();
      });
      // Give the compositor a beat so the frame isn't black.
      await new Promise((r) => requestAnimationFrame(() => r(null)));

      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx || !canvas.width || !canvas.height) {
        stopStream(stream);
        throw new Error("Could not read a video frame from the shared screen.");
      }
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      stopStream(stream); // stop sharing immediately after grabbing one frame
      video.srcObject = null;

      frameCanvasRef.current = canvas;
      await new Promise<void>((resolve) =>
        canvas.toBlob((blob) => {
          if (blob) setImgUrl(URL.createObjectURL(blob));
          resolve();
        }, "image/png")
      );
      setState("captured");
    },
    [stopStream]
  );

  const startScreenCapture = useCallback(async () => {
    setError("");
    setSel(null);
    setCroppedUrl("");
    croppedBlobRef.current = null;
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setState("error");
      setError(
        "Screen capture isn't supported in this browser. Try a Chromium-based browser."
      );
      return;
    }
    setState("requesting-permission");
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: false,
      });
      await captureFrameFromStream(stream);
    } catch (e) {
      const err = e as DOMException;
      setState("error");
      if (err?.name === "NotAllowedError") {
        setError("Screen sharing was cancelled or denied.");
      } else {
        setError(err?.message || "Screen capture failed.");
      }
    }
  }, [captureFrameFromStream]);

  // Kick off capture on mount.
  useEffect(() => {
    startScreenCapture();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Revoke object URLs on unmount.
  useEffect(() => {
    return () => {
      if (imgUrl) URL.revokeObjectURL(imgUrl);
      if (croppedUrl) URL.revokeObjectURL(croppedUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imgUrl, croppedUrl]);

  // ---- Region selection (pointer, any direction) ----
  function relPoint(e: React.PointerEvent): { x: number; y: number } {
    const r = imgRef.current!.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(r.width, e.clientX - r.left)),
      y: Math.max(0, Math.min(r.height, e.clientY - r.top)),
    };
  }

  function beginSelection(e: React.PointerEvent) {
    if (state !== "captured" && state !== "selecting" && state !== "cropped") return;
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    dragStart.current = relPoint(e);
    setCroppedUrl("");
    croppedBlobRef.current = null;
    setSel({ x: dragStart.current.x, y: dragStart.current.y, w: 0, h: 0 });
    setState("selecting");
  }

  function updateSelection(e: React.PointerEvent) {
    if (state !== "selecting" || !dragStart.current) return;
    const p = relPoint(e);
    const s = dragStart.current;
    setSel({
      x: Math.min(s.x, p.x),
      y: Math.min(s.y, p.y),
      w: Math.abs(p.x - s.x),
      h: Math.abs(p.y - s.y),
    });
  }

  function finishSelection() {
    if (state !== "selecting") return;
    dragStart.current = null;
    setState("captured");
    // Auto-crop once a valid region is drawn.
    setTimeout(cropSelectedRegion, 0);
  }

  const cropSelectedRegion = useCallback(() => {
    const frame = frameCanvasRef.current;
    const img = imgRef.current;
    if (!frame || !img || !sel) return;
    const rect = img.getBoundingClientRect();
    const scaleX = frame.width / rect.width;
    const scaleY = frame.height / rect.height;
    const sx = Math.round(sel.x * scaleX);
    const sy = Math.round(sel.y * scaleY);
    const sw = Math.round(sel.w * scaleX);
    const sh = Math.round(sel.h * scaleY);
    if (sw < MIN_CROP || sh < MIN_CROP) {
      setCroppedUrl("");
      croppedBlobRef.current = null;
      return;
    }
    const out = document.createElement("canvas");
    out.width = sw;
    out.height = sh;
    const ctx = out.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(frame, sx, sy, sw, sh, 0, 0, sw, sh);
    out.toBlob((blob) => {
      if (!blob) return;
      croppedBlobRef.current = blob;
      setCroppedUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return URL.createObjectURL(blob);
      });
    }, "image/png");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);

  async function sendCroppedImageToAI() {
    const blob = croppedBlobRef.current;
    if (!blob) return;
    setState("sending");
    try {
      const file = new File([blob], `screenshot-${Date.now()}.png`, {
        type: "image/png",
      });
      await onAttach(file);
      onClose();
    } catch (e) {
      setState("error");
      setError((e as Error).message || "Failed to attach the screenshot.");
    }
  }

  const hasValidCrop = !!croppedBlobRef.current;

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/80 p-4">
      <div className="mx-auto flex w-full max-w-6xl flex-1 flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <div className="text-sm font-semibold">
            📸 Screenshot capture
            <span className="ml-2 font-normal text-gray-500">
              {state === "requesting-permission" && "Waiting for screen share…"}
              {(state === "captured" || state === "selecting") &&
                "Drag to select a region"}
              {state === "sending" && "Attaching…"}
              {state === "error" && "Something went wrong"}
            </span>
          </div>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            ✕
          </button>
        </div>

        <div className="relative flex-1 overflow-auto bg-gray-100 p-3 dark:bg-gray-950">
          {state === "requesting-permission" && (
            <div className="flex h-full items-center justify-center text-sm text-gray-500">
              Choose a screen, window, or tab to share in the browser prompt…
            </div>
          )}

          {state === "error" && (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
              <div className="text-sm text-red-500">{error}</div>
              <button
                onClick={startScreenCapture}
                className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110"
              >
                Try again
              </button>
            </div>
          )}

          {imgUrl && state !== "error" && (
            <div className="flex items-start justify-center">
              <div
                className="relative inline-block select-none"
                onPointerDown={beginSelection}
                onPointerMove={updateSelection}
                onPointerUp={finishSelection}
              >
                <img
                  ref={imgRef}
                  src={imgUrl}
                  alt="Captured screen"
                  draggable={false}
                  className="max-h-[60vh] max-w-full cursor-crosshair rounded border border-gray-300 dark:border-gray-700"
                />
                {sel && (sel.w > 1 || sel.h > 1) && (
                  <div
                    className="pointer-events-none absolute border-2 border-brand bg-brand/10"
                    style={{
                      left: sel.x,
                      top: sel.y,
                      width: sel.w,
                      height: sel.h,
                    }}
                  />
                )}
              </div>
            </div>
          )}
        </div>

        {/* Cropped preview + actions */}
        <div className="flex items-center gap-3 border-t border-gray-200 px-4 py-2 dark:border-gray-700">
          {croppedUrl ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">Selected region:</span>
              <img
                src={croppedUrl}
                alt="Cropped region"
                className="max-h-16 rounded border border-gray-300 dark:border-gray-700"
              />
            </div>
          ) : (
            <span className="text-xs text-gray-400">
              {imgUrl ? "Drag on the image to select a region." : ""}
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={startScreenCapture}
              disabled={state === "requesting-permission" || state === "sending"}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-40 dark:border-gray-600"
            >
              Retake
            </button>
            <button
              onClick={onClose}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600"
            >
              Cancel
            </button>
            <button
              onClick={sendCroppedImageToAI}
              disabled={!hasValidCrop || state === "sending"}
              className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
            >
              {state === "sending" ? "Attaching…" : "Attach to message"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
