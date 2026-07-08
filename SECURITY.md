# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report it privately via GitHub's
[private vulnerability reporting](https://github.com/zmustafa/MultiChat/security/advisories/new)
(Security → Report a vulnerability). Include:

- a description of the issue and its impact,
- steps to reproduce (proof of concept if possible),
- affected component/version.

You'll get an acknowledgement as soon as possible, and we'll work with you on a fix
and coordinated disclosure.

## Scope & hardening notes

MultiChat's local defaults are meant for development, **not** direct public exposure:

- Set a strong random `JWT_SECRET` and a unique `APP_ENCRYPTION_KEY`.
- Change the seeded **admin/admin** account before exposing the app.
- Run behind HTTPS / a reverse proxy; restrict `FRONTEND_ORIGIN`.
- Provider keys and tool secrets are Fernet-encrypted at rest and proxied by the
  backend (never sent to the browser). `fetch_url`/`web_search` block private,
  loopback, and link-local targets and cap response size.

Do not commit real secrets: `.env` and `*.db` are gitignored on purpose.
