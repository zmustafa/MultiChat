import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch, clearEtagCache, getToken, setToken } from "../api/client";
import type { User } from "../api/types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const queryClient = useQueryClient();
  // Auth actions also supersede work when the token has not changed yet (pending login).
  const authVersion = useRef(0);

  const logout = useCallback(() => {
    authVersion.current++;
    setToken(null);
    // clear() destroys/cancels pending queries, even when their fetch ignores AbortSignal.
    queryClient.clear();
    setUser(null);
    setLoading(false);
    clearEtagCache();
    localStorage.removeItem("multichat_active");
    localStorage.removeItem("multichat_last");
  }, [queryClient]);

  const refreshUser = useCallback(async () => {
    const version = authVersion.current;
    const token = getToken();
    if (!token) return;
    const isCurrent = () => version === authVersion.current && token === getToken();
    try {
      const res = await apiFetch<{ user: User }>("/api/auth/me");
      if (isCurrent()) setUser(res.user);
    } catch (err) {
      // A network interruption or temporary 5xx must not destroy a valid 30-day login.
      // Only an authoritative authentication failure invalidates the stored token.
      if (isCurrent() && err instanceof ApiError && err.status === 401) logout();
    }
  }, [logout]);

  useEffect(() => {
    // Keep the ref object: cleanup must invalidate the latest action, not restore an old version.
    const lifecycle = authVersion;
    const version = lifecycle.current;
    void refreshUser().finally(() => {
      if (version === lifecycle.current) setLoading(false);
    });
    // Ignore startup/login/refresh completions after unmount or StrictMode replay.
    return () => { lifecycle.current++; };
  }, [refreshUser]);

  async function login(email: string, password: string) {
    const version = ++authVersion.current;
    const token = getToken();
    try {
      const res = await apiFetch<{ token: string; user: User }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      if (version !== authVersion.current || token !== getToken()) {
        throw new DOMException("Authentication changed during sign-in", "AbortError");
      }
      setToken(res.token);
      // Sign-in can replace an account without a preceding logout. Purge before publishing it.
      queryClient.clear();
      clearEtagCache();
      setUser(res.user);
    } finally {
      if (version === authVersion.current) setLoading(false);
    }
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
