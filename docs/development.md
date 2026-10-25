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
