import { useSyncExternalStore } from "react";

export interface Artifact {
  id: string;
  title: string;
  content: string;
  created_at: number;
}

const KEY = "multichat_artifacts";
const listeners = new Set<() => void>();

function read(): Artifact[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "[]");
  } catch {
    return [];
  }
}

let cache: Artifact[] = read();

function write(next: Artifact[]) {
  cache = next;
  localStorage.setItem(KEY, JSON.stringify(next));
  listeners.forEach((l) => l());
}

export function addArtifact(content: string, title?: string) {
  const a: Artifact = {
    id: crypto.randomUUID(),
    title: title || content.slice(0, 40).replace(/\n/g, " ") || "Artifact",
    content,
    created_at: Date.now(),
  };
  write([a, ...cache]);
}

export function removeArtifact(id: string) {
  write(cache.filter((a) => a.id !== id));
}

export function useArtifacts(): Artifact[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => cache
  );
}
