import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { useProviders } from "./hooks/useProviders";
import { AppLayout } from "./components/AppLayout";
import { DefaultPasswordPrompt } from "./components/DefaultPasswordPrompt";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { ComparePage } from "./pages/ComparePage";
import { EvalsPage } from "./pages/EvalsPage";
import { GeneralSettingsPage } from "./pages/GeneralSettingsPage";
import { SecuritySettingsPage } from "./pages/SecuritySettingsPage";
import { IntegrationsPage } from "./pages/IntegrationsPage";
import { LoginPage } from "./pages/LoginPage";
import { PersonaLibraryPage } from "./pages/PersonaLibraryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SnippetLibraryPage } from "./pages/SnippetLibraryPage";

function Protected({ children }: { children: JSX.Element }) {
  const { user, loading } = useAuth();
  if (loading)
    return (
      <div className="flex h-full items-center justify-center text-sm text-gray-500">
        Loading…
      </div>
    );
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

/**
 * Nothing works without an AI provider, so gate the main app on having at least one.
 * When none are configured, send the user to Settings where an explicit prompt guides
 * them to add their first provider.
 */
function RequireProvider({ children }: { children: JSX.Element }) {
  const { data: providers, isLoading, isSuccess } = useProviders();
  if (isLoading)
    return (
      <div className="flex h-full items-center justify-center text-sm text-gray-500">
        Loading…
      </div>
    );
  if (isSuccess && (providers?.length ?? 0) === 0)
    return <Navigate to="/settings?setup=1" replace />;
  return children;
}

export default function App() {
  const { user } = useAuth();
  return (
    <>
      <DefaultPasswordPrompt />
      <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
      <Route
        path="/"
        element={
          <Protected>
            <RequireProvider>
              <ComparePage />
            </RequireProvider>
          </Protected>
        }
      />
      <Route
        path="/c/:sessionId"
        element={
          <Protected>
            <RequireProvider>
              <ComparePage />
            </RequireProvider>
          </Protected>
        }
      />
      <Route
        path="/settings"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<SettingsPage />} />
        <Route path="general" element={<GeneralSettingsPage />} />
        <Route path="security" element={<SecuritySettingsPage />} />
      </Route>
      <Route
        path="/personas"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<PersonaLibraryPage />} />
      </Route>
      <Route
        path="/snippets"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<SnippetLibraryPage />} />
      </Route>
      <Route
        path="/analytics"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<AnalyticsPage />} />
      </Route>
      <Route
        path="/evals"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<EvalsPage />} />
      </Route>
      <Route
        path="/integrations"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<IntegrationsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </>
  );
}
