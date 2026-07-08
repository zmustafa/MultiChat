# Contributing to MultiChat

Thanks for your interest in contributing! 🎉

## Getting set up

See the [README](README.md) for local development instructions (backend = FastAPI on
`:5001`, frontend = Vite on `:5000`).

## Before you open a pull request

Please make sure the checks below pass locally.

**Frontend** (`frontend/`):

```bash
npm install
npm run typecheck   # tsc --noEmit
npm run build       # production build must succeed
```

**Backend** (`backend/`):

```bash
python -m compileall app          # no syntax errors
# optional if you use them:
ruff check .
```

## Guidelines

- Keep changes focused; one logical change per PR.
- Match the existing code style (TypeScript + Tailwind on the frontend, typed Python +
  FastAPI on the backend). Don't reformat unrelated code.
- Don't add heavy dependencies without discussion — the frontend deliberately avoids a
  charting library and keeps the bundle lean.
- Never commit secrets, `.env` files, or `*.db` databases.
- Update the README/docs when you change user-facing behavior or configuration.

## Reporting bugs / requesting features

Use the issue templates under **Issues → New issue**. For security problems, follow
[SECURITY.md](SECURITY.md) instead of filing a public issue.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
