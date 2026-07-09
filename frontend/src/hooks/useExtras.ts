import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { Folder, SearchHit, Snippet } from "../api/types";

// ---------------- Folders ----------------

export function useFolders() {
  return useQuery({
    queryKey: ["folders"],
    queryFn: () => apiFetch<Folder[]>("/api/folders"),
  });
}

export function useFolderMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["folders"] });
  const create = useMutation({
    mutationFn: (name: string) =>
      apiFetch<Folder>("/api/folders", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      apiFetch<Folder>(`/api/folders/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/folders/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  return { create, update, remove };
}

// ---------------- Snippets ----------------

export function useSnippets() {
  return useQuery({
    queryKey: ["snippets"],
    queryFn: () => apiFetch<Snippet[]>("/api/snippets"),
  });
}

export function useSnippetMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["snippets"] });
  const create = useMutation({
    mutationFn: (body: { title: string; content: string }) =>
      apiFetch<Snippet>("/api/snippets", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Snippet> }) =>
      apiFetch<Snippet>(`/api/snippets/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/snippets/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
  return { create, update, remove };
}

// ---------------- Settings (custom instructions) ----------------

export interface UserSettingsData {
  custom_instructions: string | null;
  new_chat_use_default_persona: boolean;
}

export function useUserSettings() {
  return useQuery({
    queryKey: ["userSettings"],
    queryFn: () => apiFetch<UserSettingsData>("/api/settings"),
  });
}

export function useSettingsMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<UserSettingsData>) =>
      apiFetch<UserSettingsData>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["userSettings"] }),
  });
}

// ---------------- Search ----------------

export function searchSessions(q: string): Promise<SearchHit[]> {
  return apiFetch<SearchHit[]>(`/api/sessions/search?q=${encodeURIComponent(q)}`);
}
