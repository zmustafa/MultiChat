import { ChangePasswordForm } from "../components/ChangePasswordForm";
import { SettingsShell } from "../components/SettingsShell";
import { useAuth } from "../auth/AuthContext";

/** Settings → Security (change account password). */
export function SecuritySettingsPage() {
  const { user } = useAuth();
  return (
    <SettingsShell title="Security">
      <div className="mx-auto max-w-5xl p-4">
        <section className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
          <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
            <h2 className="font-medium">Change password</h2>
            <p className="mt-0.5 text-xs text-gray-500">
              Update the password for your account ({user?.email}).
            </p>
          </div>
          <div className="p-5">
            {user?.is_default_password && (
              <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
                You're still using the default seeded password. Set a new one to secure your
                account.
              </div>
            )}
            <ChangePasswordForm />
          </div>
        </section>
      </div>
    </SettingsShell>
  );
}
