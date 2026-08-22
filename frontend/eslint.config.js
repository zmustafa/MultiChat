import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

// Deliberately narrow: this exists to catch real defects (stale-closure hook deps,
// unreachable code, accidental `any` shadowing) rather than to restyle working code.
export default tseslint.config(
  { ignores: ["dist", "node_modules", "*.config.js", "*.config.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // The codebase uses `any` at the react-markdown / SSE payload boundaries, where the
      // upstream types are genuinely unknown. Flagging every one is noise.
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // Missing deps are the stale-closure bug class; existing suppressions stay explicit.
      "react-hooks/exhaustive-deps": "warn",
    },
  },
);
