// @vitest-environment node
import type { ComponentProps, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "../pages/LoginPage";
import { ChangePasswordForm } from "./ChangePasswordForm";
import { SessionSidebar } from "./SessionSidebar";

// Keep the components, router, query hooks and React renderer real; mock only auth.
const auth = vi.hoisted(() => ({
  user: { id: "demo", email: "admin" },
  refreshUser: vi.fn<() => Promise<void>>(),
  login: vi.fn<(email: string, password: string) => Promise<void>>(),
}));
vi.mock("../auth/AuthContext", () => ({ useAuth: () => auth }));

const fetchMock = vi.fn<typeof fetch>();
let queryClient: QueryClient;
const noop = () => undefined;
const emptySidebarProps: ComponentProps<typeof SessionSidebar> = {
  sessions: [],
  personas: [],
  activeId: null,
  onSelect: noop,
  onNew: noop,
  onRename: noop,
  onDelete: noop,
  onCollapse: noop,
};

beforeEach(() => {
  auth.user.email = "admin";
  auth.refreshUser.mockReset().mockResolvedValue(undefined);
  auth.login.mockReset().mockResolvedValue(undefined);
  const storage = new Map<string, string>([
    ["multichat_token", "stored-admin-token"],
    ["multichat_user", JSON.stringify({ id: "demo", email: "admin" })],
    ["multichat_active", "admin-session"],
    ["username", "admin"],
    ["email", "admin"],
    ["password", "saved-password-not-for-rendering"],
  ]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
    clear: () => storage.clear(),
    key: (index: number) => [...storage.keys()][index] ?? null,
    get length() { return storage.size; },
  } satisfies Storage);
  vi.stubGlobal("fetch", fetchMock.mockReset().mockImplementation(() => {
    throw new Error("Unexpected network request in an autofill SSR test");
  }));
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false, gcTime: Infinity },
    },
  });
});

afterEach(() => {
  try {
    // Also catch unexpected requests whose errors a component or query swallowed.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(auth.login).not.toHaveBeenCalled();
    expect(auth.refreshUser).not.toHaveBeenCalled();
  } finally {
    queryClient.clear();
    vi.unstubAllGlobals();
  }
});

function render(children: ReactNode): string {
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={["/settings/security"]}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>,
  );
}

// These helpers inspect React's serialized attributes, not browser DOM behavior.
// SSR never runs the debounced search effect; real typing/autofill needs a browser check.
function inputs(markup: string): string[] {
  return markup.match(/<input\b[^>]*>/g) ?? [];
}

function attribute(tag: string, name: string): string | undefined {
  return new RegExp(`\\s${name}="([^"]*)"`, "i").exec(tag)?.[1];
}

function inputByAttribute(markup: string, name: string, value: string): string {
  const matches = inputs(markup).filter((input) => attribute(input, name) === value);
  expect(matches, `one input with ${name}="${value}"`).toHaveLength(1);
  return matches[0];
}

function expectAssociatedLabel(markup: string, input: string) {
  const id = attribute(input, "id");
  expect(id, "a nonempty input id").toBeTruthy();
  const labels = markup.match(/<label\b[^>]*>[\s\S]*?<\/label>/g) ?? [];
  const linked = labels.filter((label) => attribute(label, "for") === id);
  expect(linked, `one label for input ${id}`).toHaveLength(1);
  expect(linked[0].replace(/<[^>]*>/g, "").trim()).not.toBe("");
}

function searchInput(): string {
  const rendered = inputs(render(<SessionSidebar {...emptySidebarProps} />));
  expect(rendered).toHaveLength(1);
  return rendered[0];
}

describe("SessionSidebar search autofill markup", () => {
  it("identifies the field as chat search with an accessible name", () => {
    const input = searchInput();
    expect.soft(attribute(input, "name")).toBe("chat-search");
    expect.soft(attribute(input, "type")).toBe("search");
    expect.soft(attribute(input, "aria-label")).toBe("Search chats");
  });

  it("opts out of saved credentials and password-manager suggestions", () => {
    const input = searchInput();
    expect.soft(attribute(input, "autocomplete")).toBe("off");
    expect.soft(attribute(input, "data-lpignore")).toBe("true");
    expect.soft(attribute(input, "data-1p-ignore")).toBe("true");
  });

  it("starts empty even when stored credentials and the current user contain admin", () => {
    expect(localStorage.getItem("multichat_token")).toContain("admin");
    expect(auth.user.email).toBe("admin");
    expect(attribute(searchInput(), "value")).toBe("");
  });

  it("does not block typing with readonly or disabled attributes", () => {
    const input = searchInput();
    expect(attribute(input, "readonly")).toBeUndefined();
    expect(attribute(input, "disabled")).toBeUndefined();
  });

  it("gives two simultaneously rendered sidebars distinct nonempty ids", () => {
    const rendered = inputs(render(<>
      <SessionSidebar {...emptySidebarProps} />
      <SessionSidebar {...emptySidebarProps} />
    </>));
    expect(rendered).toHaveLength(2);
    const ids = rendered.map((input) => attribute(input, "id"));
    for (const id of ids) expect(id, "a nonempty search id").toBeTruthy();
    // Do not couple the contract to React's internal useId string format.
    expect(new Set(ids).size).toBe(2);
  });
});

describe.each([
  { variant: "normal", compact: false },
  { variant: "compact", compact: true },
])("ChangePasswordForm ($variant)", ({ compact }) => {
  it.each(["admin", "member@example.test"])(
    "includes a hidden, readonly username from the current user (%s)",
    (email) => {
      auth.user.email = email;
      const markup = render(<ChangePasswordForm compact={compact} />);
      const identity = inputByAttribute(markup, "name", "username");
      expect(attribute(identity, "type")).toBe("text");
      expect(attribute(identity, "autocomplete")).toBe("username");
      expect(attribute(identity, "hidden")).toBeDefined();
      expect(attribute(identity, "readonly")).toBeDefined();
      expect(attribute(identity, "value")).toBe(email);
      expectAssociatedLabel(markup, identity);
    },
  );

  it("retains labeled password fields, autofill hints and explicit field names", () => {
    const markup = render(<ChangePasswordForm compact={compact} />);
    const passwords = inputs(markup).filter((input) => attribute(input, "type") === "password");
    expect(passwords).toHaveLength(3);
    expect.soft(passwords.map((input) => attribute(input, "name"))).toEqual([
      "current_password", "new_password", "confirm_password",
    ]);
    expect(passwords.map((input) => attribute(input, "autocomplete"))).toEqual([
      "current-password", "new-password", "new-password",
    ]);
    for (const input of passwords) {
      expectAssociatedLabel(markup, input);
      expect(attribute(input, "required")).toBeDefined();
      expect(attribute(input, "readonly")).toBeUndefined();
      expect(attribute(input, "disabled")).toBeUndefined();
    }
  });

  it("renders empty passwords without submitting or signaling success", () => {
    const onSuccess = vi.fn();
    const markup = render(<ChangePasswordForm compact={compact} onSuccess={onSuccess} />);
    const passwords = inputs(markup).filter((input) => attribute(input, "type") === "password");
    expect(passwords).toHaveLength(3);
    for (const input of passwords) expect(attribute(input, "value")).toBe("");
    expect(markup).not.toContain("saved-password-not-for-rendering");
    expect(onSuccess).not.toHaveBeenCalled();
    // afterEach also asserts that no request, login or refresh occurred.
  });
});

it("keeps all identity/password ids and label associations unique across normal and compact forms", () => {
  const markup = render(<><ChangePasswordForm /><ChangePasswordForm compact /></>);
  const forms = markup.match(/<form\b[^>]*>[\s\S]*?<\/form>/g) ?? [];
  expect(forms).toHaveLength(2);
  const ids: string[] = [];
  for (const form of forms) {
    const fields = inputs(form);
    expect(fields).toHaveLength(4);
    inputByAttribute(form, "name", "username");
    for (const input of fields) {
      expectAssociatedLabel(form, input);
      ids.push(attribute(input, "id")!);
    }
  }
  expect(new Set(ids).size).toBe(8);
});

describe("LoginPage keeps credential autofill enabled", () => {
  it.each([
    { hint: "username", type: "text" },
    { hint: "current-password", type: "password" },
  ])("preserves the labeled, editable $hint field", ({ hint, type }) => {
    const markup = render(<LoginPage />);
    const input = inputByAttribute(markup, "autocomplete", hint);
    expect(inputs(markup)).toHaveLength(2);
    expect(attribute(input, "type")).toBe(type);
    expect(attribute(input, "value")).toBe("");
    expect(attribute(input, "required")).toBeDefined();
    expect(attribute(input, "readonly")).toBeUndefined();
    expect(attribute(input, "disabled")).toBeUndefined();
    expect(attribute(input, "data-lpignore")).toBeUndefined();
    expect(attribute(input, "data-1p-ignore")).toBeUndefined();
    expectAssociatedLabel(markup, input);
  });
});