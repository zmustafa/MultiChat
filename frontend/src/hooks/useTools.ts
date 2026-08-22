import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "../api/client";

export interface ToolDef {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

/**
 * The tools the backend actually has registered.
 *
 * The tool list used to be hardcoded in the UI, so adding a tool to the backend registry
 * without editing that array left it invisible and un-toggleable.
 */
export function useTools() {
  return useQuery({
    queryKey: ["tools"],
    queryFn: () => apiFetch<ToolDef[]>("/api/tools"),
    staleTime: 5 * 60 * 1000,
  });
}
