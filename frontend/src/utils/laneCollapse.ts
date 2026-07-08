const KEY = "multichat_lane_collapsed";

function readMap(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}

function writeMap(m: Record<string, boolean>): void {
  localStorage.setItem(KEY, JSON.stringify(m));
}

/** Whether a lane should render collapsed (persisted across reloads). */
export function isLaneCollapsed(laneId: string): boolean {
  return !!readMap()[laneId];
}

/** Persist a lane's collapsed/expanded state. */
export function setLaneCollapsedState(laneId: string, collapsed: boolean): void {
  const m = readMap();
  if (collapsed) m[laneId] = true;
  else delete m[laneId];
  writeMap(m);
}

/**
 * Seed the collapse map for freshly-created lanes (e.g. launching a persona) so
 * they appear minimized by default when the compare window opens.
 * `laneIds` and `collapsedFlags` are index-aligned.
 */
export function seedLaneCollapse(laneIds: string[], collapsedFlags: boolean[]): void {
  const m = readMap();
  laneIds.forEach((id, i) => {
    if (collapsedFlags[i]) m[id] = true;
    else delete m[id];
  });
  writeMap(m);
}
