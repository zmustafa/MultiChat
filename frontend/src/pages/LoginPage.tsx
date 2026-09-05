import { useState } from "react";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      await login(email, password);
      nav("/");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-gray-100 dark:bg-gray-950">
      <form
        onSubmit={submit}
        className="w-80 space-y-3 rounded-lg bg-white p-6 shadow dark:bg-gray-900"
      >
        <h1 className="text-xl font-semibold">Sign in to MultiChat</h1>
        <label htmlFor="login-email" className="sr-only">Email or username</label>
        <input
          id="login-email"
          type="text"
          placeholder="Email or username"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
        <label htmlFor="login-password" className="sr-only">Password</label>
        <input
          id="login-password"
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
        {error && <div role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</div>}
        <button
          disabled={busy}
          className="w-full rounded bg-blue-700 py-2 text-sm font-medium text-white disabled:opacity-60"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
