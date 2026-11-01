# Development

AI-assisted development starts at [AI_CONTEXT.md](../AI_CONTEXT.md). Use its task routes before reading full docs or source trees.

## Prerequisites

- Rust stable with rustfmt and Clippy
- Python 3.11 or newer
- .NET 8 SDK
- PowerShell 7.4 or newer (`pwsh`, not Windows PowerShell 5.1)

These are developer prerequisites. The production distribution must bundle or manage the
runtime and tools; this repository layout does not require end users to install them manually.
Runtime/model packaging and the final dependency/license audit are still release work.

## Layout

- `core/`: Rust core, authenticated desktop session, Job API, Song Data and Python supervision
- `python/`: offline analysis service with a replaceable model adapter interface
- `gui/OpenKaraoke.Client/`: UI-independent .NET client, owned Core launcher and ViewModel
- `gui/OpenKaraoke.Gui/`: Avalonia settings, connection and Job shell
- `gui/OpenKaraoke.Client.Tests/`: HTTP/ViewModel contracts and actual Core/Python integration
- `gui/OpenKaraoke.Gui.Smoke/`: test-only headless XAML rendering and GUI/Core workflow
- `scripts/`: Windows developer and CI entry points

## Build and checks

From the repository root in PowerShell 7.4+:

```powershell
./scripts/build.ps1
./scripts/check.ps1
./scripts/dependency-inventory.ps1
```

`check.ps1` propagates every native command failure. It runs Rust formatting, Clippy and tests,
Python lint/format/pytest, a real Rust-to-Python lifecycle/crash-recovery test, the Avalonia
build and .NET formatter verification. The cross-process test is explicitly invoked with
`cargo test --test job_api -- --ignored`; it is not silently skipped in CI.

The additional desktop contract runner is intentionally explicit about unit-only vs live mode:

```powershell
dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- --unit
$core = (Resolve-Path target/debug/open-karaoke-core.exe).Path
$python = (Get-Command python).Source
dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- $core $python
dotnet run --project gui/OpenKaraoke.Gui.Smoke --configuration Release -- artifacts/desktop-smoke $core $python
```

On Linux use `target/debug/open-karaoke-core` and the corresponding absolute Python path.
The dedicated desktop workflow runs the real process tests and headless GUI workflow on both
Windows and Linux. See [UI validation](desktop_ui_validation.md) for the evidence boundary.
CI permissions are read-only. Dependency inventories are review inputs, not a completed audit.

## Local pre-PR check

Run one command before opening or updating a PR:

```powershell
pwsh scripts/check_local.ps1
```

It compares `origin/main` (no fetch; run `git fetch` yourself) with committed, uncommitted and
untracked changes, then selects the checks automatically:

| Changed | Checks |
|---|---|
| any | `git diff --check` (also untracked files; with `-All` / `-ChangedFiles` the selected files) |
| `*.py`, `python/pyproject.toml` | `ruff check` / `ruff format --check` on the changed files (all of `python` when `pyproject.toml` changes) |
| `python/**` | matching `python/tests/test_<module>*.py`; shared contracts, fixtures or unmatched modules run all of `python/tests`. Real-model/live-Core opt-in variables are cleared; a run where nothing passed is `UNVERIFIED` |
| `core/**`, `Cargo.*` | `cargo fmt --check`, `cargo clippy --offline -D warnings`, `cargo test --offline` (no `--ignored`) |
| `gui/**` | `dotnet build --no-restore`, `dotnet format --verify-no-changes`, `OpenKaraoke.Client.Tests --unit` |
| `*.ps1` / `*.md` | PowerShell parse, relative Markdown links |
| `.github/**`, others | reported as `UNVERIFIED` (CI only / no light check) |

The summary lists `PASS` / `FAIL` / `SKIP` / `UNVERIFIED` with the reason and the rerun command.
Exit code `0` means every required check passed, `1` means a failure, `2` means no changed
files were found, and `3` means a required check could not be verified (for example all selected
tests were skipped). None of them is reported as PASS; use `-All` to check everything. Missing tools or
dependencies are failures; the script never installs packages, restores NuGet, fixes files or
touches the git index. Logs go to `artifacts/check_local/`. Python is resolved from `-Python`,
`OPEN_KARAOKE_DEV_PYTHON`, `.venv` in the checkout, then `.venv` of the main checkout (for
worktrees), then `python`; `python/src` of the checkout is put first on `PYTHONPATH`.

`-PlanOnly` prints the selection, `-SelfTest` verifies the selection rules and that an injected
syntax error, failing test and broken link are reported as FAIL (also run in Windows CI). Heavy
checks stay in CI: the `--ignored` Core-to-Python process test, desktop live/smoke runs, real
models, Linux and packaging.

## Python trust tooling (#66)

Every Python environment that installs project dependencies must use `pip >= 26.2.2` and
`certifi >= 2026.10.19`. `uv >= 0.13.4` and `sigstore >= 4.5.1` are required only when used;
the repository uses neither today. Upgrade first, then verify:

```powershell
python -m pip install --upgrade "pip>=26.2.2" "certifi>=2026.10.19"
python scripts/check_python_trust.py
```

`check_python_trust.py` prints the Python executable, the pip/certifi/uv/sigstore versions, the
CA bundle that certifi resolves to, the standard-library default verify paths and any
`SSL_CERT_FILE`/`SSL_CERT_DIR`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` override, and exits non-zero when
a minimum is not met. `build.ps1` and `check.ps1` run both commands before installing the
package, and every GitHub Actions job that calls `actions/setup-python` also calls
`.github/actions/python-trust` (guarded by `python/tests/test_python_trust.py`).

Existing virtual environments are not repaired by editing `pyproject.toml`: re-run the two
commands in each one. Model weight downloads (`urllib`) use the operating system trust store,
not certifi, so the OS certificate update state must be checked separately. Whether a specific
affected certificate chain is still trusted has to be confirmed against the real CA bundle in
use; passing this check only proves the minimum versions and shows which bundle is read.

## Distribution provenance and verification (#67)

Inventory (2026-11-02): the repository has **no GitHub Release and no published installer**.
Other workflows upload only test reports, metrics, screenshots and dependency inventories;
those are review evidence, not deliverables. The only distributable outputs are produced by
[`distribution.yml`](../.github/workflows/distribution.yml) (push to `main`, same-repo PRs,
manual run):

| File | Content |
|---|---|
| `open-karaoke-core-windows-x64.zip` | `open-karaoke-core.exe` (Rust, `--release --locked`) |
| `open-karaoke-gui-windows-x64.zip` | `dotnet publish` output of `OpenKaraoke.Gui` (framework-dependent .NET 8) |
| `open_karaoke_analysis-*.whl` / `.tar.gz` | Python Analysis Service wheel and sdist |
| `SHA256SUMS` | SHA-256 of the four files above |

Each file, including `SHA256SUMS`, gets a GitHub keyless Artifact Attestation (SLSA build
provenance signed through Sigstore with the workflow's OIDC identity; no key is stored in the
repository). The `attest` job verifies every checksum and attestation, checks that a tampered
copy is rejected, and only then uploads the `open-karaoke-distribution` artifact. A failed
verification fails the job, so nothing unverified is uploaded. Fork PRs have no OIDC token and
produce no distribution.

To verify a downloaded `open-karaoke-distribution` artifact (extract the outer zip that GitHub
wraps around the artifact first; the attested subjects are the files inside it):

```powershell
gh attestation verify <file> --repo tomiya7688/Open_karaoke `
  --signer-workflow tomiya7688/Open_karaoke/.github/workflows/distribution.yml
Get-FileHash -Algorithm SHA256 <file>   # compare with the line in SHA256SUMS
```

On Linux/macOS use `sha256sum -c SHA256SUMS`. Verify `SHA256SUMS` with the same
`gh attestation verify` command before trusting it. The attestation proves which repository,
workflow, commit and GitHub-hosted runner built the file; it is not a code-signing certificate.

**Unverified / blocked:** the `.exe`/`.dll` files are **not Authenticode-signed** and carry no
RFC 3161 timestamp, so Windows shows the publisher as Unknown. This needs a trusted code
signing certificate (or a signing service such as Azure Trusted Signing) provided by the
maintainer as a CI secret; no placeholder key is committed. Until then these outputs are CI
build candidates, not a public release. macOS, Android and container outputs are not produced.

## Run Core and GUI

From the repository root:

```powershell
python -m pip install -e './python[dev]'
$env:OPEN_KARAOKE_PYTHON = (Get-Command python).Source
cargo build --locked -p open-karaoke-core
$env:OPEN_KARAOKE_CORE = (Resolve-Path target/debug/open-karaoke-core.exe).Path
dotnet run --project gui/OpenKaraoke.Gui/OpenKaraoke.Gui.csproj
```

For Linux, set the same non-secret executable paths using the shell's environment syntax.
Alternatively enter the absolute paths in the GUI settings tab, then choose Coreに接続.
The GUI launches and owns Core; Core supervises its private Python worker. **Do not start a
second unauthenticated Core in another terminal.** Direct `cargo run` now requires the
[session pipe bootstrap](desktop_session.md); it is no longer the desktop launch procedure.
No token must be typed into a command line or persisted in environment/configuration.

The initial GUI supports connection state and a clearly labelled **mock communication Job**
with progress/cancellation/errors. It is not yet a functioning karaoke player. See
[Desktop client](desktop_client.md) for usage and [Core API](core_api.md) for authentication.

The worker registers stems, lyrics, alignment, offline pitch, Vocal Events, score notes and
`song-pipeline-v1` in addition to mock. [Full-song analysis](full_song_analysis.md) accepts an
already normalized WAV and exports a SongBundle and asset descriptors. Raw-file normalization
and atomic, audio-inclusive SongStore registration are not yet connected to this Job or GUI.
Real inference usually requires optional model runtimes and verified weights; adapter
registration is not model installation. See [Current status](current_status.md),
[Analysis Service](analysis_service.md), and each feature's optional dependency instructions.
