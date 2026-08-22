import { lazy, Suspense, type ReactElement } from "react";
import { Navigate, Route, Routes } from "react-router";
import { useAuth } from "./auth/AuthContext";
import { useProviders } from "./hooks/useProviders";
import { AppLayout } from "./components/AppLayout";
import { DefaultPasswordPrompt } from "./components/DefaultPasswordPrompt";
import { ComparePage } from "./pages/ComparePage";
import { LoginPage } from "./pages/LoginPage";

// Secondary routes are loaded on demand: bundled eagerly they added the analytics,
// evals, benchmark, persona, snippet, integrations and provider-settings pages to the
// entry chunk that every chat session has to download before first paint.
const AnalyticsPage = lazy(() =>
  import("./pages/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })),
);
const BenchmarkPage = lazy(() =>
  import("./pages/BenchmarkPage").then((m) => ({ default: m.BenchmarkPage })),
);
const EvalsPage = lazy(() =>
  import("./pages/EvalsPage").then((m) => ({ default: m.EvalsPage })),
);
const GeneralSettingsPage = lazy(() =>
  import("./pages/GeneralSettingsPage").then((m) => ({ default: m.GeneralSettingsPage })),
);
const SecuritySettingsPage = lazy(() =>
  import("./pages/SecuritySettingsPage").then((m) => ({ default: m.SecuritySettingsPage })),
);
const IntegrationsPage = lazy(() =>
  import("./pages/IntegrationsPage").then((m) => ({ default: m.IntegrationsPage })),
);
const PersonaLibraryPage = lazy(() =>
  import("./pages/PersonaLibraryPage").then((m) => ({ default: m.PersonaLibraryPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);
const SnippetLibraryPage = lazy(() =>
  import("./pages/SnippetLibraryPage").then((m) => ({ default: m.SnippetLibraryPage })),
);

function PageFallback() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-gray-500">
      Loading…
    </div>
  );
}

function Protected({ children }: { children: ReactElement }) {
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
function RequireProvider({ children }: { children: ReactElement }) {
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
      <Suspense fallback={<PageFallback />}>
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
        path="/d/:runId"
        element={
          <Protected>
            <RequireProvider>
              <ComparePage />
            </RequireProvider>
          </Protected>
        }
      />
      <Route
        path="/benchmark"
        element={
          <Protected>
            <RequireProvider>
              <BenchmarkPage />
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
      </Suspense>
    </>
  );
}
