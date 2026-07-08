import { PersonalSettings } from "../components/PersonalSettings";
import { SettingsShell } from "../components/SettingsShell";
import { SystemBackup } from "../components/SystemBackup";

/** Settings → General (custom instructions, full-system backup, other preferences). */
export function GeneralSettingsPage() {
  return (
    <SettingsShell title="General">
      <PersonalSettings />
      <SystemBackup />
    </SettingsShell>
  );
}
