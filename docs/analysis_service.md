# Python Analysis Service (Issue #5)

## Implemented boundary

Rust Core owns process startup, the public Job IDs, polling, cancellation, and failure reporting.
Python is an offline worker; it is never called from an audio callback. The service uses FastAPI,
Pydantic and Uvicorn. Startup imports no ML runtime and downloads no model weights.

The only included adapter is `mock-v1` (role `mock`). Stem separation, ASR, alignment, pitch,
notes and song generation are reserved endpoints, **not implemented inference**. Until a real
adapter is registered they return HTTP 501 / `not_implemented`, not fabricated outputs.

## Start

Development prerequisites: Rust stable, Python >=3.11, .NET 8 and PowerShell >=7.4.

```powershell
python -m pip install -e './python[dev]'
$env:OPEN_KARAOKE_PYTHON = (Get-Command python).Source
$env:OPEN_KARAOKE_DATA_DIR = "$env:LOCALAPPDATA/OpenKaraoke"
cargo run -p open-karaoke-core
```

`OPEN_KARAOKE_BIND` defaults to `127.0.0.1:32145`; non-loopback addresses are rejected.
Core prefers `runtime/python/python.exe` beside its executable on Windows, then a developer
`python` on PATH, unless `OPEN_KARAOKE_PYTHON` is set. Packaging the runtime and ML models is
still release work; no end-user manual Python installation is prescribed by this interface.

The Python child binds `127.0.0.1` on an OS-assigned port before publishing a readiness file.
Core checks child PID, instance UUID, protocol version 1 and authenticated health. Startup has
an explicit 30-second deadline. Local HTTP uses a 3-second timeout, no proxy and no redirects.
A fresh random bearer token is passed via the child environment, never the command line,
readiness file or public health response. Python rejects browser Origin headers and requests
larger than 64 KiB. Its private port is not a browser-facing web application.

## Public Core API

- `GET /health`: Core health, independent of Python.
- `GET /analysis/health`: authenticated child health, or 503 while unavailable/recovering.
- `GET /models`: registered model descriptors, forwarded by Core.
- Existing `/jobs`, `/jobs/{id}`, `/jobs/{id}/cancel`, `/jobs/{id}/events` (SSE).

Submit a fixture through **Core**, not directly to the Python port:

```powershell
$job = Invoke-RestMethod http://127.0.0.1:32145/jobs -Method Post -ContentType application/json -Body @'
{"kind":"analysis.mock","analysis":{"model_id":"mock-v1","options":{"steps":5,"delay_ms":10}}}
'@
Invoke-RestMethod "http://127.0.0.1:32145/jobs/$($job.id)"
```

The Core ID differs from the private Python ID. Progress, stage, artifacts and terminal state
are mapped back to the Core job. Python errors keep a stable `code` and safe `message`; Core
uses the existing string error field (`code: message`) for backwards compatibility. Existing
framework-smoke jobs remain available. Core SSE sends an initial snapshot when subscribing.

## Private service contracts

`GET /health`, `GET /models`, `POST /analysis/{role}`, `GET /jobs/{uuid}`,
`POST /jobs/{uuid}/cancel`, `POST /shutdown`. All require the launch token.

Roles: `mock`, `stems`, `lyrics`, `alignment`, `pitch`, `notes`, `song`.
An analysis request contains optional `model_id`, optional `input_artifact`, and `options`.
Unknown fields and invalid role-specific options fail validation. Structured HTTP errors:

```json
{"error":{"code":"model_not_found","message":"Requested model is not registered","retryable":false}}
```

Snapshots include `instance_id`, `id`, `kind`, `model_id`, status, progress, stage, timestamps,
optional structured error, and artifact references. The queue retains at most 256 records,
evicts only finished records and returns 429 when all slots are live. There is one inference
worker by default; the HTTP event loop is separate from synchronous adapter execution.

## Adapter extension

Register trusted `ModelAdapter` objects using `create_app(root, token, adapters=[...])`.
Each supplies `ModelInfo`, `validate_options` and synchronous `analyze(context, options)`.
The registry rejects duplicate IDs and mismatched roles. On-disk package discovery/selection
is Issue #17, not silently mixed into this foundation.

Adapters call `context.checkpoint()` between inference chunks and report progress using
`context.report()`. The cancellation event is thread-safe. Cancelling a task does not pretend
to stop arbitrary native model code: uncooperative/hung code ultimately requires terminating
the child. No unfinished artifact is advertised as a completed result.

## Artifacts

Core supplies one artifact root. Inputs are normalized relative paths below it; parent
traversal, absolute paths, Windows drives/ADS, backslashes and symlinks are rejected on all OSes.
Results live below `jobs/<service-instance>/<private-job-id>/`. Adapters use `write_json` for
atomic, deterministic JSON publication. Failed/cancelled job directories are cleaned before
terminal completion is exposed. The Core receives relative references only; this issue does
not mutate SongStore documents or add a library/import GUI.

Adapters are trusted executable code, not a sandbox. The artifact directory must be private
to the OS account. Checks do not provide protection against a hostile same-user process
racing filesystem replacements. A hard process crash may leave an unreferenced job directory;
job results are not recovered or automatically replayed, and stale-directory GC is deferred.

## Process recovery and persistence

Core checks child exit/health every 500 ms. Three failed health checks trigger recovery;
an observed exit does so immediately. Restart attempts are capped at three for the Core
lifetime with increasing backoff. A stopped/exhausted service makes new analysis unavailable.
Each job pins one child instance: interrupted jobs become `failed`; only new jobs use a new
instance. This prevents duplicate work or accidental association with a recycled private ID.

On normal Core shutdown, Python stops admission, signals cancellation and drains workers.
After the grace period Core kills and reaps the child. `kill_on_drop` also guards early-return
paths. Job records are in-memory and do not survive process restart. An individual Core
analysis job has a one-hour execution deadline; model-specific budgeting is future work.

## Validation

```powershell
python -m pytest python/tests -q
cargo test --workspace --all-targets
cargo test --test job_api -- --ignored
./scripts/check.ps1
```

The explicitly invoked integration test starts the real Python child from Rust, exercises
Core health/models and mock jobs, checks cancellation and structured failure, kills the
owned child, verifies automatic recovery without replay, executes a new job and shuts down.
Python tests cover authentication, structured validation, path rejection, adapter replacement,
queue limits, deterministic artifacts, cleanup, subprocess startup and graceful shutdown.

## Build corrections carried with this issue

The earlier Windows CI reported success despite Rust compilation errors because native
command exit codes were not propagated by `check.ps1`. Checks now stop on nonzero exits and
Python tests actually run. The malformed newline byte literal in SongStore is repaired;
formatting and Clippy findings are corrected rather than suppressing the lint gate.

## Primary implementation references

- FastAPI lifespan: https://fastapi.tiangolo.com/advanced/events/
- Tokio child lifecycle: https://docs.rs/tokio/latest/tokio/process/struct.Child.html
- Reqwest client: https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html
- PowerShell native exit handling: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_preference_variables
