import type { Persona, PersonaLane, Provider } from "../api/types";

export interface ResolvedLane {
  provider_id: string;
  model: string;
  role: PersonaLane["role"];
  collapsed: boolean;
}

/**
 * Resolve a persona's lanes to concrete provider ids against the user's configured
 * providers. Starter/prebuilt personas ship with portable model *hints* (a model name and
 * role, with an empty or stale provider id), so at launch time we bind each lane to a
 * provider that actually offers that model. Lanes that can't be resolved are dropped; if
 * nothing resolves, we fall back to a single lane on the default provider so the launched
 * chat is still usable.
 */
export function resolvePersonaLanes(
  persona: Persona,
  providers: Provider[],
): ResolvedLane[] {
  const validIds = new Set(providers.map((p) => p.id));
  const resolved: ResolvedLane[] = [];

  for (const lane of persona.lanes) {
    // Keep an already-valid provider id as-is.
    if (lane.provider_id && validIds.has(lane.provider_id)) {
      resolved.push({
        provider_id: lane.provider_id,
        model: lane.model,
        role: lane.role,
        collapsed: !!lane.collapsed,
      });
      continue;
    }
    // Otherwise bind the model hint to a provider that offers that model.
    const byModel = providers.find((p) => (p.models || []).includes(lane.model));
    if (byModel) {
      resolved.push({
        provider_id: byModel.id,
        model: lane.model,
        role: lane.role,
        collapsed: !!lane.collapsed,
      });
    }
    // Unresolvable model hint → drop the lane.
  }

  // Guarantee a usable chat: if no lane resolved but the user has a provider, add one.
  if (resolved.length === 0 && providers.length > 0) {
    const dp = providers.find((p) => p.is_default) || providers[0];
    const model = dp.default_model || (dp.models || [])[0];
    if (model) {
      resolved.push({ provider_id: dp.id, model, role: "responder", collapsed: false });
    }
  }

  return resolved;
}
