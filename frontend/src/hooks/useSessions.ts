import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type {
  Lane,
  LaneRole,
  SessionDetail,
  SessionListItem,
} from "../api/types";

export function useSessions() {
  return useQuery({
    queryKey: ["sessions"],
    queryFn: () => apiFetch<SessionListItem[]>("/api/sessions"),
  });
}

export function useSession(id: string | null) {
  return useQuery({
    queryKey: ["session", id],
    queryFn: () => apiFetch<SessionDetail>(`/api/sessions/${id}`),
    enabled: !!id,
  });
}

export function useSessionMutations() {
  const qc = useQueryClient();
  const invalidateList = () => qc.invalidateQueries({ queryKey: ["sessions"] });
  const invalidateOne = (id: string) =>
    qc.invalidateQueries({ queryKey: ["session", id] });

  const create = useMutation({
    mutationFn: (body: {
      title?: string;
      system_prompt?: string;
      lanes: { provider_id: string; model: string; role?: LaneRole }[];
      tools_enabled?: boolean;
      tool_config?: Record<string, unknown>;
    }) =>
      apiFetch<SessionDetail>("/api/sessions", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: invalidateList,
  });

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      apiFetch<SessionDetail>(`/api/sessions/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (_d, v) => {
      invalidateList();
      invalidateOne(v.id);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/sessions/${id}`, { method: "DELETE" }),
    onSuccess: invalidateList,
  });

  const addLane = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: { provider_id: string; model: string; role?: LaneRole };
    }) =>
      apiFetch<Lane>(`/api/sessions/${id}/lanes`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (_d, v) => invalidateOne(v.id),
  });

  const removeLane = useMutation({
    mutationFn: ({ id, laneId }: { id: string; laneId: string }) =>
      apiFetch<void>(`/api/sessions/${id}/lanes/${laneId}`, {
        method: "DELETE",
      }),
    onSuccess: (_d, v) => invalidateOne(v.id),
  });

  const updateLane = useMutation({
    mutationFn: ({
      id,
      laneId,
      body,
    }: {
      id: string;
      laneId: string;
      body: { provider_id?: string; model?: string; hidden?: boolean; state?: string };
    }) =>
      apiFetch<Lane>(`/api/sessions/${id}/lanes/${laneId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (_d, v) => invalidateOne(v.id),
  });

  const deleteTurn = useMutation({
    mutationFn: ({ id, turnId }: { id: string; turnId: string }) =>
      apiFetch<void>(`/api/sessions/${id}/turns/${turnId}`, { method: "DELETE" }),
    onSuccess: (_d, v) => invalidateOne(v.id),
  });

  const autotitle = useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ title: string }>(`/api/sessions/${id}/autotitle`, {
        method: "POST",
      }),
    onSuccess: (_d, id) => {
      invalidateList();
      invalidateOne(id);
    },
  });

  const emptyTrash = useMutation({
    mutationFn: () =>
      apiFetch<void>(`/api/sessions/trash/empty`, { method: "DELETE" }),
    onSuccess: () => invalidateList(),
  });

  return {
    create,
    update,
    remove,
    addLane,
    removeLane,
    updateLane,
    deleteTurn,
    autotitle,
    emptyTrash,
  };
}
