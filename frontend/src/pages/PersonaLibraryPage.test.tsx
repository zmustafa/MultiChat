// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Persona, PersonaLane, Provider } from "../api/types";
import { PersonaLaneCopy } from "../components/PersonaLaneCopy";
import { PersonaLibraryPage } from "./PersonaLibraryPage";

// Keep real components, query/mutation hooks and API serialization; only fake auth and HTTP.
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "test-user", email: "test@example.test" }, logout: vi.fn() }),
}));

const provider: Provider = {
  id: "provider-a", name: "Test provider", provider_type: "openai", auth_method: "api_key",
  base_url: null, masked_key: null, has_key: true, oauth_connected: false, oauth_expires_at: null,
  models: ["model-a", "model-b", "original-model"], default_model: "model-a", extra: {},
  is_default: true, created_at: "2026-09-05T00:00:00Z",
};

function persona(id: string, name: string, lanes: PersonaLane[]): Persona {
  return {
    id, name, description: `${name} description`, system_prompt: `${name} prompt`,
    notice: `${name} notice`, tools_enabled: false, tool_config: { disabled: ["web_search"] },
    is_default: false, lanes, deliberation: null,
    created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:00Z",
  };
}

let source: Persona;
let target: Persona;
let stored: Persona[];
let clients: QueryClient[];
let writes: { path: string; method: string; body: Partial<Persona> }[];
let unexpected: string[];
let failSave: boolean;
let saveGate: Promise<void> | undefined;

beforeEach(() => {
  source = persona("source", "Source persona", [
    { provider_id: provider.id, model: "model-b", role: "responder", collapsed: true },
    { provider_id: "", model: "model-a", role: "responder", collapsed: false },
    { provider_id: provider.id, model: "model-a", role: "judge", collapsed: true },
  ]);
  source.tools_enabled = true;
  source.deliberation = {
    mode: "council", max_rounds: 3, synthesis: true, minority_report: true,
    critique_synthesis: true, evidence: true,
  };
  target = persona("target", "Target persona", [
    { provider_id: provider.id, model: "original-model", role: "responder", collapsed: false },
  ]);
  target.is_default = true;
  stored = [source, target, persona("empty", "Empty persona", [])];
  clients = [];
  writes = [];
  unexpected = [];
  failSave = false;
  saveGate = undefined;
  localStorage.clear();
  localStorage.setItem("multichat_token", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://localhost").pathname;
    const method = init?.method ?? "GET";
    if (method === "GET" && path === "/api/personas") return Response.json(stored);
    if (method === "GET" && path === "/api/providers") return Response.json([provider]);
    if ((method === "PATCH" && path === "/api/personas/target") ||
        (method === "POST" && path === "/api/personas")) {
      const body = JSON.parse(String(init?.body)) as Partial<Persona>;
      writes.push({ path, method, body });
      await saveGate;
      if (failSave) return Response.json({ detail: "Save unavailable" }, { status: 503 });
      const saved = { ...(method === "POST" ? persona("new", "", []) : target), ...body };
      stored = method === "POST" ? [...stored, saved] : stored.map((p) => p.id === "target" ? saved : p);
      return Response.json(saved);
    }
    unexpected.push(`${method} ${path}`);
    return Response.json({ detail: "Unexpected request" }, { status: 500 });
  }));
});

afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  vi.unstubAllGlobals();
  expect(unexpected).toEqual([]);
});

function renderLibrary() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity }, mutations: { retry: false, gcTime: Infinity },
    },
  });
  clients.push(client);
  return render(
    <MemoryRouter initialEntries={["/personas"]}>
      <QueryClientProvider client={client}><PersonaLibraryPage /></QueryClientProvider>
    </MemoryRouter>,
  );
}

async function editTarget(user: ReturnType<typeof userEvent.setup>) {
  const heading = await screen.findByRole("heading", { name: "Target persona" });
  const card = heading.parentElement!.parentElement!;
  await user.click(within(card).getByRole("button", { name: "Edit" }));
  return within(screen.getByRole("dialog", { name: "Edit — Target persona" }));
}

async function selectSource(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Copy lanes from…" }));
  await user.click(screen.getByRole("radio", { name: "Source persona 3 lanes" }));
}

describe("lane copy picker", () => {
  function renderPicker(overrides: Partial<Parameters<typeof PersonaLaneCopy>[0]> = {}) {
    const onCopy = vi.fn<(lanes: PersonaLane[]) => void>();
    const props = {
      personas: stored, targetId: target.id, targetName: target.name,
      providers: [provider], providersReady: true, onCopy, ...overrides,
    };
    return { ...render(<PersonaLaneCopy {...props} />), onCopy, props };
  }

  it("excludes the target, disables empty sources, and searches by name/description", async () => {
    const user = userEvent.setup();
    renderPicker();
    await user.click(screen.getByRole("button", { name: "Copy lanes from…" }));
    expect(screen.queryByRole("radio", { name: /Target persona/ })).toBeNull();
    expect((screen.getByRole("radio", { name: /Empty persona/ }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Replace lanes" }) as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByRole("searchbox"), "SOURCE PERSONA DESCRIPTION");
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "not found");
    expect(screen.getByText("No matching personas.")).toBeTruthy();
  });

  it("previews lane order, providers, roles and collapsed states without applying anything", async () => {
    const user = userEvent.setup();
    const { onCopy } = renderPicker();
    await selectSource(user);
    const rows = within(screen.getByRole("list", { name: "Lanes to copy" })).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "1. model-bTest provider · Responder · Starts minimized",
      "2. model-aAutomatic provider · Responder · Starts expanded",
      "3. model-aTest provider · Judge · Starts minimized",
    ]);
    expect(screen.getByText(/Replace all current lanes in “Target persona”/)).toBeTruthy();
    expect(onCopy).not.toHaveBeenCalled();
  });

  it("copies all values verbatim into independent objects without modifying its source", async () => {
    const user = userEvent.setup();
    const original = structuredClone(source);
    source.lanes.forEach(Object.freeze);
    Object.freeze(source.lanes);
    const { onCopy } = renderPicker();
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    const copied = onCopy.mock.calls[0][0];
    expect(copied).toEqual(source.lanes);
    expect(copied).not.toBe(source.lanes);
    copied.forEach((lane, index) => expect(lane).not.toBe(source.lanes[index]));
    copied[0].model = "changed";
    copied.pop();
    expect(source).toEqual(original);
    expect(screen.getByRole("status").textContent).toContain("Save persona to keep them");
  });

  it("warns about missing bindings without resolving, dropping or substituting lanes", async () => {
    const user = userEvent.setup();
    source.lanes[0].provider_id = "deleted-provider";
    source.lanes[1].model = "missing-auto-model";
    source.lanes[2].model = "unlisted-model";
    const { onCopy } = renderPicker();
    await selectSource(user);
    expect(screen.getByText("This provider is no longer available.")).toBeTruthy();
    expect(screen.getByText("No configured provider lists this automatic model hint.")).toBeTruthy();
    expect(screen.getByText(/This model is not in the provider's current model list/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    expect(onCopy).toHaveBeenCalledWith(source.lanes);
  });

  it("does not call a configured default model unavailable just because the model list is empty", async () => {
    const user = userEvent.setup();
    source.lanes = [{ provider_id: provider.id, model: provider.default_model!, role: "responder" }];
    renderPicker({ providers: [{ ...provider, models: [] }] });
    await user.click(screen.getByRole("button", { name: "Copy lanes from…" }));
    await user.click(screen.getByRole("radio", { name: /Source persona/ }));
    expect(screen.queryByText(/This model is not in/)).toBeNull();
  });

  it("handles provider loading without false availability checks", async () => {
    const user = userEvent.setup();
    renderPicker({ providers: [], providersReady: false });
    await selectSource(user);
    expect(screen.getByText(/availability cannot be checked yet/)).toBeTruthy();
    expect(screen.queryByText("This provider is no longer available.")).toBeNull();
  });

  it("cancels the selection without applying any lanes", async () => {
    const user = userEvent.setup();
    const { onCopy } = renderPicker();
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Cancel copy" }));
    expect(onCopy).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "Copy lane configuration" })).toBeNull();
  });

  it("blocks oversized sources rather than silently truncating them", async () => {
    const user = userEvent.setup();
    source.lanes = Array.from({ length: 7 }, (_, i) => ({ ...source.lanes[0], model: `model-${i}` }));
    const { onCopy } = renderPicker();
    await user.click(screen.getByRole("button", { name: "Copy lanes from…" }));
    await user.click(screen.getByRole("radio", { name: /Source persona/ }));
    expect(screen.getByRole("alert").textContent).toContain("no lanes will be dropped");
    expect(screen.getAllByRole("listitem")).toHaveLength(7);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    expect(onCopy).not.toHaveBeenCalled();
  });

  it("handles an empty library and a source removed during selection", async () => {
    const user = userEvent.setup();
    const { rerender, props, onCopy } = renderPicker();
    await selectSource(user);
    rerender(<PersonaLaneCopy {...props} personas={[target]} />);
    expect(screen.getByText("No other personas available.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    expect(onCopy).not.toHaveBeenCalled();
  });
});

describe("persona editor lane copy persistence", () => {
  it.each([false, true])("replaces only lanes, preserves other fields and reloads correctly (panel=%s)", async (panel) => {
    if (panel) target.deliberation = { ...source.deliberation!, mode: "quick", evidence: false };
    const before = structuredClone(stored);
    const user = userEvent.setup();
    const view = renderLibrary();
    const editor = await editTarget(user);
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    expect(writes).toEqual([]);
    expect(editor.queryByRole("button", { name: /Remove lane.*original-model/ })).toBeNull();
    await user.click(editor.getByRole("button", { name: "Save persona" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(writes).toHaveLength(1);
    expect(writes[0]).toEqual({
      path: "/api/personas/target", method: "PATCH", body: {
        name: target.name, description: target.description, system_prompt: target.system_prompt,
        notice: target.notice, tools_enabled: target.tools_enabled, tool_config: target.tool_config,
        lanes: source.lanes, deliberation: target.deliberation,
      },
    });
    expect(stored.find((p) => p.id === "target")).toEqual({ ...before[1], lanes: source.lanes });
    expect(stored.find((p) => p.id === "source")).toEqual(before[0]);
    view.unmount();
    renderLibrary(); // New query client, re-fetch from fake persistence rather than an optimistic cache.
    const reopened = await editTarget(user);
    expect(reopened.getAllByRole("button", { name: /^Remove lane/ }).map((b) => b.getAttribute("aria-label")))
      .toEqual(["Remove lane 1: model-b", "Remove lane 2: model-a", "Remove lane 3: model-a"]);
    expect(reopened.getAllByTitle("Starts minimized — click to start expanded")).toHaveLength(2);
  });

  it("discards copied lanes when the editor is canceled", async () => {
    const user = userEvent.setup();
    const before = structuredClone(stored);
    renderLibrary();
    const editor = await editTarget(user);
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    await user.click(editor.getByRole("button", { name: "Cancel" }));
    const reopened = await editTarget(user);
    expect(reopened.getByRole("button", { name: "Remove lane 1: original-model" })).toBeTruthy();
    expect(writes).toEqual([]);
    expect(stored).toEqual(before);
  });

  it("leaves copied lanes editable while keeping the source intact", async () => {
    const user = userEvent.setup();
    const beforeSource = structuredClone(source);
    renderLibrary();
    const editor = await editTarget(user);
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    await user.click(editor.getAllByTitle("Move left")[1]);
    await user.click(editor.getByRole("button", { name: "Remove lane 2: model-b" }));
    await user.click(editor.getByTitle("Starts expanded — click to start minimized"));
    await user.click(editor.getByRole("button", { name: "Save persona" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(writes[0].body.lanes).toEqual([
      { ...beforeSource.lanes[1], collapsed: true }, beforeSource.lanes[2],
    ]);
    expect(source).toEqual(beforeSource);
  });

  it("retains the draft on save failure and supports retry", async () => {
    const user = userEvent.setup();
    failSave = true;
    renderLibrary();
    const editor = await editTarget(user);
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    await user.click(editor.getByRole("button", { name: "Save persona" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Your draft is still here");
    expect(editor.getAllByRole("button", { name: /^Remove lane/ })).toHaveLength(3);
    expect(stored[1].lanes).toEqual(target.lanes);
    failSave = false;
    await user.click(editor.getByRole("button", { name: "Save persona" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(writes).toHaveLength(2);
    expect(writes[1].body.lanes).toEqual(source.lanes);
  });

  it("blocks duplicate saves and closing while the save is pending", async () => {
    let release!: () => void;
    saveGate = new Promise<void>((resolve) => { release = resolve; });
    const user = userEvent.setup();
    renderLibrary();
    const editor = await editTarget(user);
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    await user.click(editor.getByRole("button", { name: "Save persona" }));
    const saving = await editor.findByRole("button", { name: "Saving…" });
    expect((saving as HTMLButtonElement).disabled).toBe(true);
    await user.click(saving);
    await user.click(editor.getByRole("button", { name: "Cancel" }));
    await user.click(editor.getByRole("button", { name: "Close persona editor" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(writes).toHaveLength(1);
    await act(async () => { release(); });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("also copies into a new persona without creating anything until Save", async () => {
    const user = userEvent.setup();
    renderLibrary();
    await screen.findByRole("heading", { name: "Target persona" });
    await user.click(screen.getByRole("button", { name: "+ New persona" }));
    await selectSource(user);
    await user.click(screen.getByRole("button", { name: "Replace lanes" }));
    expect(writes).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Save persona" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(writes[0].method).toBe("POST");
    expect(writes[0].body).toMatchObject({ name: "New persona", lanes: source.lanes, deliberation: null });
  });
});