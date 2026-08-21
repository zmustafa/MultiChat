/** Offered next to an empty prompt box so a blank screen still shows what it is for.
 *  Clicking one fills the box; you still press Send. */
export const STARTER_PROMPTS = [
  "Explain the trade-offs of event-driven vs request/response architecture.",
  "Review this design for security risks and rank them by severity.",
  "Draft a one-page summary I can send to a non-technical stakeholder.",
];

/** A persona carrying a system prompt leads with a prompt that makes each lane
 *  introduce itself in that role. */
export const PERSONA_STARTER =
  "Based on your instructions, what are you set up to help me with?";

/** The starters to offer for a given persona (or none, for a blank topic). */
export function startersFor(systemPrompt?: string | null): string[] {
  return systemPrompt?.trim()
    ? [PERSONA_STARTER, ...STARTER_PROMPTS.slice(0, 2)]
    : STARTER_PROMPTS;
}
