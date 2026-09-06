// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Provider } from "../api/types";
import { ModelPicker } from "./ModelPicker";

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: "provider-a",
    name: "Provider A",
    provider_type: "anthropic",
    auth_method: "oauth",
    base_url: null,
    masked_key: null,
    has_key: false,
    oauth_connected: true,
    oauth_expires_at: null,
    models: ["stale-first-model", "working-default-model"],
    default_model: "working-default-model",
    extra: {},
    is_default: true,
    created_at: "2026-09-06T00:00:00Z",
    ...overrides,
  };
}

afterEach(cleanup);

describe("ModelPicker", () => {
  it("selects the provider default instead of the first catalogue model", async () => {
    const onAdd = vi.fn();
    render(<ModelPicker providers={[provider()]} onAdd={onAdd} />);

    const modelPicker = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    await waitFor(() => expect(modelPicker.value).toBe("working-default-model"));

    await userEvent.click(screen.getByRole("button", { name: "Add lane" }));
    expect(onAdd).toHaveBeenCalledWith(
      "provider-a",
      "working-default-model",
      "responder",
    );
  });

  it("falls back to the first listed model when the default is not listed", async () => {
    render(
      <ModelPicker
        providers={[provider({ default_model: "unlisted-model" })]}
        onAdd={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect((screen.getAllByRole("combobox")[1] as HTMLSelectElement).value).toBe(
        "stale-first-model",
      ),
    );
  });
});
