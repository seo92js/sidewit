# Deployment and recovery

Sidewit is one API container plus an optional web UI. It needs no database: Contexts and
Tools are JSON files in a data directory that you mount as a volume (see
[SPEC §11](SPEC.md#11-storage)). It never connects to an application database and never executes
SQL.

## What the data directory holds

- `contexts/*.json`: schema, descriptions, Business Rules, allowed values and metric contracts.
- `tools/<tool_id>.json`: reviewed SQL templates and their input contracts. Existing name-based
  files remain supported in place; names are stored inside the files and can be changed in the UI/API.

Treat the directory and its backups as sensitive: Contexts describe your data model and may
list real code values. Generations (questions, generated SQL, parameter values) are kept only
in process memory and are lost on restart. Generation logs contain metadata only, never questions, SQL or values. Saved Tool templates
and Context literals remain on disk and may contain private values. Questions and Context content are sent to the configured LLM Provider,
whose retention Sidewit does not control.

## Run

1. Keep the API private. The supplied Compose file publishes web and API on loopback only.
   Configure HTTPS at your ingress before allowing remote traffic.
2. Mount a persistent directory at `SIDEWIT_DATA_DIR` (default `/data`), writable by the
   container user (see [data directory owner](#data-directory-owner)). Run **one API process**
   per directory on a local filesystem; multiple Uvicorn
   workers and network filesystems are not supported. A lifetime `.lock` prevents a second
   process from opening the same directory. Do not delete this lock file while the API runs.
3. Set non-default `SIDEWIT_ADMIN_USERNAME` and `SIDEWIT_ADMIN_PASSWORD`, a nonempty
   `SIDEWIT_API_KEY`, and the chosen LLM provider, model and key in private
   deployment configuration. Provider keys are not API authentication keys. Never bake
   credentials into an image. Do not give the Sidewit API key to end users.
4. Start the service and wait for `/api/v1/health` (it checks that the data directory is
   readable and writable). If any data file is invalid, the API refuses to start and prints
   the file and the reason.
5. Run the [Quick Start](../README.md#quick-start) through your ingress and confirm Tool
   draft, save and invoke as well as Generate.

The web UI is optional; the API alone provides every function.

Compose restarts both containers unless you stop them (`restart: unless-stopped`). Their
health checks (Docker Engine 25+ for `start_interval`) probe every second while starting, so `docker compose up --wait` returns as soon
as they are ready, then every 30 seconds. Each API probe is a logged `/api/v1/health` request.

### Data directory owner

The API image runs as the unprivileged UID/GID 1000, which owns `/data` in the image. Compose
runs it as `SIDEWIT_UID:SIDEWIT_GID` (default `1000:1000`); set these to the owner of
`SIDEWIT_DATA_PATH` on the host. On Linux, create the directory as the user that will back it
up (`mkdir -p data`) and set `SIDEWIT_UID` and `SIDEWIT_GID` in `.env` to that user's
`id -u` and `id -g`. If Docker creates a missing bind-mount directory itself, it belongs to root and the API cannot
start (`Data directory /data is not usable by UID ...`). Docker Desktop on macOS and Windows
maps bind-mount ownership to your user, so the defaults work there. Outside Compose, run the
container with `--user UID:GID` for a bind mount, or keep the default user with a named volume.

New data files get the mode of a normal file creation (`0666` minus the umask; `0644` in the
image), and a replaced file keeps its existing mode, so the host owner can copy the directory
without root. To keep other host users out, restrict the directory itself, for example
`chmod 700 data`.

## Optional MCP endpoint

Set `SIDEWIT_MCP_ENABLED=true` to add `/mcp` in the existing API process. Configure a separate
`SIDEWIT_MCP_KEY` for Bearer authentication; an empty key means no authentication when enabled.
REST keys and web login do not protect MCP. The web container also proxies `/mcp`; no additional
service or shared-volume writer is needed. Remote access requires an allowed Host and an HTTPS
ingress reachable from the client. Keep Host/Origin checks enabled and preserve the incoming
Host header. See [MCP deployment and client configuration](MCP.md) for exact settings.

## Web login and recovery

The web UI has one administrator, configured entirely through environment variables:

```dotenv
SIDEWIT_ADMIN_USERNAME=admin
SIDEWIT_ADMIN_PASSWORD=admin
SIDEWIT_ADMIN_COOKIE_SECURE=false
```

These defaults also apply when no `.env` file exists. Empty administrator values use the same
defaults. The UI provides login and logout, with no signup, account database, web password
change, forced password change or generated password in logs. Change the defaults before
exposing the UI beyond localhost. Keep the supplied loopback port bindings for local use.

To change or recover credentials, edit the existing root `.env` file. If none exists, create
one from `.env.example` first; do not overwrite an existing file containing provider keys.
Set the administrator username/password, then apply the environment changes:

```bash
docker compose up -d --wait api
```

Compose recreates the API container when the configured environment changes. `docker compose
restart` alone does not reload `.env` into an existing container. With a non-Compose deployment,
update the process environment and restart the API process. The configured values are used on
every startup, including when a data directory already exists. Saved Contexts and Tools are
unaffected. API restarts invalidate all web sessions and clear temporary Generation history.

Sessions last up to 12 hours, use an HttpOnly SameSite=Lax cookie scoped to `/api/web`, and are
kept only in API process memory. Logout revokes the current browser session and clears cached
management data. Session expiry asks the user to sign in again while preserving the mounted
editor's unsaved changes. Logout from an unsaved editor uses the existing discard confirmation.
Navigation is blocked while logout is pending; a failed logout offers an explicit retry.
Failed sign-ins are limited to 10 per client address per minute; see
[client addresses behind a proxy](#client-addresses-behind-a-proxy).

Web requests use `/api/web/session` and session-protected `/api/web/v1`; they include the
`X-Sidewit-Web: 1` header to prevent cross-origin form requests. Cross-origin CORS access is not
enabled. Browsers do not store the API key or a session token in localStorage. External clients
keep using `/api/v1` with `SIDEWIT_API_KEY` when configured. A web cookie does not authenticate
the public API, and an API key does not replace web login. **Leaving the API key empty leaves
the public API unauthenticated**; web login alone does not protect that API from remote access.

For HTTPS deployments, set `SIDEWIT_ADMIN_COOKIE_SECURE=true`, use non-default administrator
credentials and a nonempty `SIDEWIT_API_KEY`, and configure TLS at your ingress. Enable the
Secure cookie setting only when the browser accesses the UI over HTTPS.

### Client addresses behind a proxy

The API identifies a client by its connection address. It takes `X-Forwarded-For` and
`X-Forwarded-Proto` into account only on connections from addresses listed in Uvicorn's
`FORWARDED_ALLOW_IPS` environment variable (default `127.0.0.1,::1`; comma-separated
addresses or CIDR ranges), and then uses the rightmost untrusted address. List only proxies
that **replace** these headers with the address they received the connection from; a proxy that
appends to them lets clients choose their own address.

- **Bundled Compose deployment.** The web container is the edge proxy: nginx sets
  `X-Forwarded-For` to the connecting address, discarding any value the client sent. Compose
  gives the web container the fixed address `${SIDEWIT_NETWORK_PREFIX}.2` (default
  `172.30.255.2`) on a dedicated `/24` network and sets `FORWARDED_ALLOW_IPS` on the API to that
  address only. Connections to the API port, including the published `127.0.0.1:8000`, keep
  their own address. If the default `172.30.255.0/24` overlaps another network, set
  `SIDEWIT_NETWORK_PREFIX` (three octets, such as `10.123.45`) and recreate the containers.
- **Another proxy in front of the web container** (for example a TLS ingress). nginx then sees
  only the ingress address, so every client shares one limit. Have the ingress replace
  `X-Forwarded-For`, and add `set_real_ip_from <ingress address>;` and
  `real_ip_header X-Forwarded-For;` to the web server configuration so that it forwards the
  client address it received.
- **Without Compose.** Point `FORWARDED_ALLOW_IPS` at your reverse proxy's address(es) and make
  that proxy replace `X-Forwarded-For`. Do not set `*` unless the API port is reachable only
  through that proxy.

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

REST, web and MCP processing emit content-free JSON operational logs with request IDs.
Compose rotates each container's local logs (`10m`, three files). Forward stdout to your
existing collector for longer retention, and save business audit records in your application
when needed. See [request correlation, privacy and retention](OBSERVABILITY.md).

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

Context updates, Tool updates, activation changes and `refresh-contract` require the last observed
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
Context version is revalidated before invoke. After a failed write or delete, the API reloads
all files once. If they cannot be reloaded, the store closes: API and MCP operations return
503 `storage_unavailable` and health checks 503, without reading the directory again. Repairs
take effect only at the next start. Stop the API, repair the directory, then start it again.
Directory listing errors also stop startup or recovery; an unreadable directory is never
treated as an empty store. Health checks test directory listing as well as temporary writes.
Storage failure logs report the operation, exception type, OS error number and recovery outcome
once per failed write; requests answered by a closed store add no further storage events.
They omit file paths, file contents and exception messages. Recovery and shutdown discard that
store's in-memory Tool validation cache and parsed Context definitions.

## Upgrades

This unreleased file-storage version does not automatically import the previous PostgreSQL
metadata. Keep the old installation and its backup until Contexts and reviewed Tools have been
exported through its API and recreated through the new API. IDs and Generation provenance are
not preserved by that manual recreation. Do not point this version at a PostgreSQL data directory.

Record the image versions you run and back up the data directory before upgrading. A newer
image validates the existing files at startup; if it rejects them, keep the old image running
on a copy of the backup until the files are migrated.

Earlier images ran the API as root and wrote data files as `0600`, so on Linux the files in a
bind-mounted directory belong to root and only root can read them. The API now runs as
`SIDEWIT_UID:SIDEWIT_GID` (default `1000:1000`) and cannot open such a directory. Before
starting the new image, stop the API and hand the directory to the account that will run it:

```bash
docker compose stop api
sudo chown -R "$(id -u):$(id -g)" ./data   # or your SIDEWIT_DATA_PATH
```

Existing files keep their owner-only `0600` mode when they are rewritten; that is enough for
the new owner. New files get the mode described above.

Then set `SIDEWIT_UID`/`SIDEWIT_GID` in `.env` to those IDs (see
[data directory owner](#data-directory-owner)) and run `docker compose up -d --build --wait`.
Docker Desktop on macOS needs no migration. Named volumes created by earlier images need
`docker run --rm -v <volume>:/data alpine chown -R 1000:1000 /data` instead.


### Browser security policy

The bundled web server sends a same-origin Content Security Policy, forbids framing, and
disables MIME sniffing. Monaco uses a local worker (blob worker URLs are permitted); styles
allow inline declarations required by the editor. API calls use the same-origin /api/ proxy.
If you customize the deployment to load remote assets or connect directly to another origin,
update CSP narrowly for those origins. These headers do not replace API authentication.
They are kept in one snippet (`frontend/nginx/sidewit-security-headers.conf`) included by every
location that sets its own headers, because nginx drops inherited `add_header` directives there.

The web server compresses static text files and sends `/index.html` with `Cache-Control:
no-cache`. Files under `/assets/` have content hashes in their names and are cached as
`immutable` for a year; a missing asset returns 404 instead of the application page, so a page
loaded before a redeployment fails visibly rather than executing HTML as a script. Reload such
a page. API and MCP responses are proxied uncompressed.
