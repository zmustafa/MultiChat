import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { Persona, PersonaLane } from "../api/types";

export function usePersonas() {
  return useQuery({
    queryKey: ["personas"],
    queryFn: () => apiFetch<Persona[]>("/api/personas"),
  });
}

export interface PersonaBody {
  name: string;
  description?: string | null;
  system_prompt?: string | null;
  tools_enabled?: boolean;
  lanes?: PersonaLane[];
}

export function usePersonaMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["personas"] });

  const create = useMutation({
    mutationFn: (body: PersonaBody) =>
      apiFetch<Persona>("/api/personas", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<PersonaBody> }) =>
      apiFetch<Persona>(`/api/personas/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/personas/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const setDefault = useMutation({
    mutationFn: ({ id, isDefault }: { id: string; isDefault: boolean }) =>
      apiFetch<Persona>(`/api/personas/${id}/default`, {
        method: "POST",
        body: JSON.stringify({ default: isDefault }),
      }),
    onSuccess: invalidate,
  });

  return { create, update, remove, setDefault };
}

export interface PersonaEnhanceBody {
  mode?: "enhance" | "generate";
  name?: string | null;
  description?: string | null;
  system_prompt?: string | null;
  instruction?: string | null;
  provider_id?: string | null;
  model?: string | null;
}

export interface PersonaEnhanceResult {
  system_prompt: string;
  used_provider: string;
  used_model: string;
}

export function usePersonaEnhance() {
  return useMutation({
    mutationFn: (body: PersonaEnhanceBody) =>
      apiFetch<PersonaEnhanceResult>("/api/personas/enhance", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
}
