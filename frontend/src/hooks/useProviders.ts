import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { Provider, TestResult } from "../api/types";

export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: () => apiFetch<Provider[]>("/api/providers"),
  });
}

export function useProviderMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["providers"] });

  const create = useMutation({
    mutationFn: (body: Partial<Provider> & { api_key?: string }) =>
      apiFetch<Provider>("/api/providers", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      apiFetch<Provider>(`/api/providers/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/providers/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const test = useMutation({
    mutationFn: (id: string) =>
      apiFetch<TestResult>(`/api/providers/${id}/test`, { method: "POST" }),
  });

  const refreshModels = useMutation({
    mutationFn: (id: string) =>
      apiFetch<string[]>(`/api/providers/${id}/models`),
    onSuccess: invalidate,
  });

  return { create, update, remove, test, refreshModels };
}
