/**
 * Remembers which persona was last used to start a chat, so the home screen opens on it.
 *
 * "blank" is stored explicitly rather than as an absent key: picking Blank is a decision,
 * and must survive a reload instead of falling back to the default persona every time.
 */
const KEY = "multichat_last_persona";

/** Stored in place of an id when the user deliberately chose a blank topic. */
export const BLANK_PERSONA = "blank";

export function rememberLastPersona(personaId: string | null) {
  localStorage.setItem(KEY, personaId ?? BLANK_PERSONA);
}

/** A persona id, BLANK_PERSONA, or null when nothing has ever been chosen. */
export function readLastPersona(): string | null {
  return localStorage.getItem(KEY);
}
