# Contributing

Sidewit is licensed under Apache-2.0. Contributions should preserve the boundary between
proposing an Operation and executing it in the calling application.

## Development checks

You need Docker for backend checks and Node.js 24 LTS with npm for frontend checks.
From the repository root:

```bash
bash scripts/check-backend.sh
(cd frontend && npm ci && npm run check)
(cd frontend && npm audit --audit-level=high)
```

The backend script runs Ruff and pytest in Docker. It starts and removes a temporary
PostgreSQL instance for validator parity tests; the product itself needs no database.
The temporary database volume and network are removed on exit; `sidewit-backend-venv` is
retained as a dependency cache between checks.
It also builds the Python wheel and renders all three generation prompts directly from that
artifact to catch missing package resources.
Tests use temporary data directories and provider fixtures, not real LLM keys.
Frontend checks run Prettier, ESLint, Vitest, TypeScript and the Vite build.
The [CI workflow](.github/workflows/checks.yml) also builds both Docker images and rejects
high/critical npm advisories, including development dependencies. Dependency audits use the
registry's current advisories, so rerun them when updating a release candidate's lockfiles.

## Language

Write documentation, specifications, code comments, prompt instructions and UI guidance in
English. Maintain one English version of each document rather than parallel translations.
Preserve original-language example questions, sample data, fixture descriptions, test inputs
and expected values when their wording matters. Do not translate API field names, identifiers,
SQL or literal values as part of a documentation-language change.
When translating headings, update links to their section anchors. Keep language-only changes
separate from functional changes.

## Pull requests

- Keep each change focused and explain the user-visible problem, behavior and validation.
- Add a regression test for behavior changes. Update the [specification](docs/SPEC.md) when
  changing contracts, and user-facing documentation when usage changes.
- Keep provider-specific response formats inside provider adapters. Unsupported required
  validation must not silently pass.
- Write PR titles and commit messages in English using `<type>: <description>`, for example
  `fix: reject conflicting Tool updates`. Use `feat`, `fix`, `docs`, `refactor`, `test` or `chore`.
- Fill in the [PR template](.github/pull_request_template.md) with a summary, validation results
  and any breaking changes. Write `None` when there are no breaking changes.
- Do not commit credentials, `.env`, user data, private logs, backups or internal review records.

Discuss new Operation types or changes to storage and execution boundaries before implementing
large changes. Use synthetic data in examples and reproductions.

## Real-provider evaluation

Real evaluations are separate from automated tests. They send fixture Contexts and questions
to the configured provider and incur API charges. Use only data you are authorized to send;
never run them automatically for untrusted pull requests.

Follow the [evaluation guide](evals/README.md). Record the model, code revision, fixture and
prompt versions, pass/fail counts and failure explanations. Keep raw outputs outside version
control; share sanitized summaries when relevant. FakeProvider results verify pipeline
behavior, not model accuracy.

## Release checks

Before release, verify Context → Generate → Tool draft/save/invoke and the
`ok`, `ambiguous` and `unanswerable` paths with both OpenAI and Gemini. If either provider
has not been tested, record `Real LLM provider E2E has not been verified.` as a release blocker.
Publish known model failures with the release notes; do not present regression pass rates
as general accuracy. Confirm loopback defaults and credential/data exclusions in Git and
Docker build contexts.

## Security issues

Follow the [security policy](SECURITY.md) rather than posting sensitive details in a public issue.
