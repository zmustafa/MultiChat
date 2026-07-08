import { ProviderSettings } from "../components/ProviderSettings";
import { SettingsShell } from "../components/SettingsShell";

/** Settings → AI Providers (connect / manage the LLM providers used by lanes). */
export function SettingsPage() {
  return (
    <SettingsShell title="AI Providers">
      <ProviderSettings />
    </SettingsShell>
  );
}
