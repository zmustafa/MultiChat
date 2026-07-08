import { useEffect, useState } from "react";
import type { Provider, LaneRole } from "../api/types";

interface Props {
  providers: Provider[];
  onAdd: (provider_id: string, model: string, role: LaneRole) => void;
  allowJudge?: boolean;
}

export function ModelPicker({ providers, onAdd, allowJudge }: Props) {
  const [providerId, setProviderId] = useState(providers[0]?.id || "");
  const [model, setModel] = useState("");
  const [role, setRole] = useState<LaneRole>("responder");

  const provider = providers.find((p) => p.id === providerId);
  const models = provider?.models || [];

  // Default the provider selection once providers load / change.
  useEffect(() => {
    if (providers.length && !providers.some((p) => p.id === providerId)) {
      setProviderId(providers[0].id);
    }
  }, [providers, providerId]);

  // Default to the provider's first model (and keep it valid when the provider changes
  // or providers finish loading).
  useEffect(() => {
    if (models.length && !models.includes(model)) {
      setModel(models[0]);
    }
  }, [providerId, models, model]);

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        value={providerId}
        onChange={(e) => {
          const next = e.target.value;
          setProviderId(next);
          setModel(providers.find((p) => p.id === next)?.models?.[0] || "");
        }}
        className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
      >
        {providers.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} ({p.provider_type})
          </option>
        ))}
      </select>
      {models.length > 0 ? (
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
        >
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      ) : (
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="model id"
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
      )}
      {allowJudge && (
        <select
          value={role}
          onChange={(e) => setRole(e.target.value as LaneRole)}
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
        >
          <option value="responder">responder</option>
          <option value="judge">judge</option>
        </select>
      )}
      <button
        disabled={!providerId || !model}
        onClick={() => {
          onAdd(providerId, model, role);
          setModel("");
        }}
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        Add lane
      </button>
    </div>
  );
}
