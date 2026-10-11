# Security policy

Sidewit is intended for private, single-team deployments. It proposes SQL Operations;
it is not an execution sandbox or a user/tenant authorization boundary.

## Deployment responsibilities

- Keep the API behind a private administrative ingress or your application. Set
  `SIDEWIT_API_KEY` and use HTTPS for remote access. The key also grants management access;
  do not distribute it to end users. The UI stores it in the administrator's localStorage.
- Enforce user and tenant authorization in the application and database. Bind parameters
  through the database driver and set appropriate role/function privileges and resource limits.
  SELECT statements can have side effects; a read-only role alone is not a complete sandbox.
- Review generated SQL and values. Context content and questions are untrusted model inputs;
  prompts and static validation do not establish business correctness or prevent every injection.

## Data handling

Sidewit connects to no database and requires no database credentials. Contexts and saved Tools
persist in the data directory and may contain private schema details, SQL literals and values.
Protect that directory and its backups.

Generation questions, SQL, parameters and rejected SQL stay in bounded process memory and are
lost on restart. Sidewit logs Generation metadata, not this content. Questions and Context
content are sent to the configured provider; review its data-handling terms before use.

## Reporting a vulnerability

If the repository offers **Security → Report a vulnerability**, use it for a private report.
A dedicated security email address has not been configured. If private reporting is unavailable,
open an issue requesting a private contact channel, without including the vulnerability details.

Share the affected revision, deployment assumptions and a minimal synthetic reproduction only
through a private channel. Never publish credentials, personal data or exploit details in an issue.

The project is pre-release. Response times and security backports are not guaranteed.
See [deployment and recovery](docs/DEPLOYMENT.md) and the [validation contract](docs/SPEC.md).
