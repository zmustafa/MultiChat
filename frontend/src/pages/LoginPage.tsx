import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await login(email, password);
      nav("/");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-gray-100 dark:bg-gray-950">
      <form
        onSubmit={submit}
        className="w-80 space-y-3 rounded-lg bg-white p-6 shadow dark:bg-gray-900"
      >
        <h1 className="text-xl font-semibold">Sign in to MultiChat</h1>
        <input
          type="text"
          placeholder="Email or username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
        {error && <div className="text-sm text-red-500">{error}</div>}
        <button className="w-full rounded bg-blue-600 py-2 text-sm font-medium text-white">
          Sign in
        </button>
      </form>
    </div>
  );
}
