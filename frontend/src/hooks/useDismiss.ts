import { useEffect } from "react";

/**
 * Dismiss a popup on an outside pointer press or the Escape key. Only listens while
 * `active` is true (i.e. the popup is open), so it's cheap when closed.
 *
 * @param ref     Element that should stay open when clicked inside (the popup + its trigger).
 * @param active  Whether the popup is currently open.
 * @param onClose Called when the user clicks outside or presses Escape.
 */
export function useDismiss(
  ref: React.RefObject<HTMLElement>,
  active: boolean,
  onClose: () => void,
): void {
  useEffect(() => {
    if (!active) return;
    const onPointer = (e: PointerEvent) => {
      const el = ref.current;
      if (el && !el.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    // Capture phase so we see the event before it can be stopped by inner handlers.
    document.addEventListener("pointerdown", onPointer, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [ref, active, onClose]);
}
