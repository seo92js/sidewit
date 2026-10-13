# Deployment and recovery

Sidewit is one API container plus an optional web UI. It needs no database: Contexts and
Tools are JSON files in a data directory that you mount as a volume (see
[SPEC §11](SPEC.md#11-storage)). It never connects to an application database and never executes
SQL.

## What the data directory holds

- `contexts/*.json`: schema, descriptions, Business Rules, allowed values and metric contracts.
- `tools/*.json`: reviewed SQL templates and their input contracts.

Treat the directory and its backups as sensitive: Contexts describe your data model and may
list real code values. Generations (questions, generated SQL, parameter values) are kept only
in process memory and are lost on restart. Generation logs contain metadata only, never questions, SQL or values. Saved Tool templates
and Context literals remain on disk and may contain private values. Questions and Context content are sent to the configured LLM Provider,
whose retention Sidewit does not control.

## Run

1. Keep the API private. The supplied Compose file publishes web and API on loopback only.
   Configure HTTPS at your ingress before allowing remote traffic.
2. Mount a persistent directory at `SIDEWIT_DATA_DIR` (default `/data`), writable by the
   container user. Run **one API process** per directory on a local filesystem; multiple Uvicorn
   workers and network filesystems are not supported. A lifetime `.lock` prevents a second
   process from opening the same directory. Do not delete this lock file while the API runs.
3. Set a nonempty `SIDEWIT_API_KEY` and the chosen LLM provider, model and key in private
   deployment configuration. Provider keys are not API authentication keys. Never bake
   credentials into an image. Do not give the Sidewit API key to end users.
4. Start the service and wait for `/api/v1/health` (it checks that the data directory is
   readable and writable). If any data file is invalid, the API refuses to start and prints
   the file and the reason.
5. Run the [Quick Start](../README.md#quick-start) through your ingress and confirm Tool
   draft, save and invoke as well as Generate.

The web UI is optional; the API alone provides every function.

## Desktop platforms

The images run Linux containers. On macOS, use Docker Desktop; on Linux, use Docker Engine
or Desktop. On Windows, use Docker Desktop's **WSL 2 backend**, Linux containers and WSL
integration for your Linux distribution. Run the Quick Start in that distribution's terminal,
with the repository under a Linux path such as `~/sidewit` and `SIDEWIT_DATA_PATH=./data`.
Keep both source and data off `/mnt/c`, OneDrive and network shares. Docker recommends
[Linux filesystem bind mounts on WSL 2](https://docs.docker.com/desktop/features/wsl/best-practices/).

Native Windows Python and Windows containers are not supported: the store uses Unix
`fcntl` file locking and directory syncing. Windows with WSL 2 is the intended Docker route,
but has not yet been acceptance-tested on a Windows host. Docker Desktop on macOS has been
tested, including persistence across container replacement and moving the data directory.

## Execution is the application's responsibility

Sidewit's validation is static (SPEC §7): it checks statement type, Context scope, names,
parameters and supported value policies. It does not check function signatures, types against
a real database, cost or side effects, and SELECT statements can have side effects. The calling
application must enforce authorization, tenant isolation, driver parameter binding (never
string interpolation), database role and function privileges, and execution time and resource
limits. If you want a database-backed check, run `EXPLAIN` in your own database before
executing. See [SECURITY.md](../SECURITY.md).

## Request budgets and limits

Generate has a 90-second deadline including the one repair attempt; each Provider call times
out at 60 seconds and SDK automatic retries are disabled. The supplied proxy waits 100
seconds and the UI 110 seconds; match any external ingress to this budget. A timeout returns
504 `generation_timeout` and may leave no Generation record.

Provider connection failures and per-call timeouts return JSON 502 `llm_error`, with a failed
Generation in the in-memory history. They do not bypass the overall 90-second deadline.

Request bodies are limited to 1 MiB, questions to 8,000 characters, DDL to 256,000
characters, SQL to 64,000 characters and Context definitions to 128 KiB of UTF-8 JSON with at
most 100 resources and 1,000 fields. These are admission limits, not a promise of accuracy or
latency at the maximum. SQL/DDL also has complexity limits: 32 levels of parentheses/brackets
(outside strings/comments), 20,000 tokens, and AST depth 64 / 10,000 nodes. Excessively complex
input returns a validation error rather than an unhandled server error.

## Versions and conflicts

Context updates, Tool updates and `refresh-contract` require the last observed
`expected_version`; conflicts return 409 and must be reconciled, not retried blindly. Invoke
optionally accepts `expected_version`; otherwise inspect the returned Tool version before
executing.

## Backup and restore

Set `SIDEWIT_DATA_PATH=/srv/sidewit-data` in your private Compose environment for the
following example, or substitute the actual host directory. The default is `./data`.
The Compose file builds the images locally; no published `sidewit-api` image is required.

Back up by copying the data directory. For a consistent copy, stop the API first:

```bash
docker compose stop api
tar -czf "sidewit-data-$(date -u +%Y%m%dT%H%M%SZ).tgz" -C /srv/sidewit-data .
docker compose start api
```

Restore by stopping the API, replacing the directory contents with the archive and starting
the API. Startup validates every file; fix or remove a file it reports before retrying. Keep
backups on protected storage. Restore into an empty directory so deleted files from an older
installation cannot survive alongside the restored files. Verify Contexts and invoke a reviewed
Tool after recovery; a successful archive extraction alone is not an application check.

To edit files by hand, stop the API first: a running instance does not reload files and may
overwrite them.

## Write failures

Each file is replaced atomically and synced. A Context update persists its new version before
saving Tool revalidation results. There is no transaction across all files: after a 503 or a
restart during a write, retrieve current versions before retrying. A Tool tied to an older
Context version is revalidated before invoke. If files cannot be reloaded after an error, API
operations fail closed with 503. Repair the directory while stopped, then restart.
Directory listing errors also stop startup or recovery; an unreadable directory is never
treated as an empty store. Health checks test directory listing as well as temporary writes.
Storage failure logs report the operation, exception type, OS error number and recovery outcome;
they omit file paths, file contents and exception messages. Recovery and shutdown discard that
store's in-memory Tool validation cache.

## Upgrades

This unreleased file-storage version does not automatically import the previous PostgreSQL
metadata. Keep the old installation and its backup until Contexts and reviewed Tools have been
exported through its API and recreated through the new API. IDs and Generation provenance are
not preserved by that manual recreation. Do not point this version at a PostgreSQL data directory.

Record the image versions you run and back up the data directory before upgrading. A newer
image validates the existing files at startup; if it rejects them, keep the old image running
on a copy of the backup until the files are migrated.


### Browser security policy

The bundled web server sends a same-origin Content Security Policy, forbids framing, and
disables MIME sniffing. Monaco uses a local worker (blob worker URLs are permitted); styles
allow inline declarations required by the editor. API calls use the same-origin /api/ proxy.
If you customize the deployment to load remote assets or connect directly to another origin,
update CSP narrowly for those origins. These headers do not replace API authentication.
