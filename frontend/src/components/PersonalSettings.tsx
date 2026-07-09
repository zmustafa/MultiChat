import { useEffect, useState } from "react";
import { useSettingsMutation, useUserSettings } from "../hooks/useExtras";

function CustomInstructions() {
  const { data } = useUserSettings();
  const save = useSettingsMutation();
  const [text, setText] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setText(data.custom_instructions || "");
  }, [data]);

  return (
    <section className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
        <h2 className="font-medium">Custom instructions</h2>
        <p className="mt-0.5 text-xs text-gray-500">
          Always prepended to the system prompt of every chat (your preferences, who you
          are, how you like answers). Applies to all lanes.
        </p>
      </div>
      <div className="p-5">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder="e.g. I'm a backend engineer. Be concise, use bullet points, prefer Python…"
          className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
        <div className="mt-2 flex items-center gap-2">
          <button
            onClick={() => {
              save.mutate({ custom_instructions: text });
              setSaved(true);
              setTimeout(() => setSaved(false), 1500);
            }}
            className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110"
          >
            Save
          </button>
          {saved && <span className="text-xs text-green-600">✓ Saved</span>}
        </div>
      </div>
    </section>
  );
}

function NewChatBehavior() {
  const { data } = useUserSettings();
  const save = useSettingsMutation();
  const enabled = !!data?.new_chat_use_default_persona;

  return (
    <section className="mt-4 rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
        <h2 className="font-medium">New chat</h2>
        <p className="mt-0.5 text-xs text-gray-500">
          Choose what happens when you click <span className="font-medium">New chat</span>.
        </p>
      </div>
      <div className="p-5">
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) =>
              save.mutate({ new_chat_use_default_persona: e.target.checked })
            }
            className="mt-0.5"
          />
          <span className="text-sm">
            <span className="block font-medium">
              Auto-start the default persona
            </span>
            <span className="block text-xs text-gray-500">
              When on, clicking New chat immediately launches your default persona. When
              off, it opens the persona picker so you can choose (or start blank).
            </span>
          </span>
        </label>
      </div>
    </section>
  );
}

export function PersonalSettings() {
  return (
    <div className="mx-auto max-w-5xl p-4">
      <CustomInstructions />
      <NewChatBehavior />
    </div>
  );
}
