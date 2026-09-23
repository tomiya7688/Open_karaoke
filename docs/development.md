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

- `core/`: Rust core, Job API, Song Data, import primitives and Python process supervision
- `python/`: offline analysis service with a replaceable model adapter interface
- `gui/`: C# / Avalonia desktop shell
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

CI runs the Rust/Python contracts on Linux and the complete check script on Windows. Its
repository permissions are read-only. Dependency inventory is uploaded from Windows, and
Python test results are uploaded from Linux. The inventory is an input to license review,
not a completed license audit.

## Run Core and GUI

```powershell
python -m pip install -e './python[dev]'
$env:OPEN_KARAOKE_PYTHON = (Get-Command python).Source
cargo run -p open-karaoke-core
# In another terminal:
dotnet run --project gui/OpenKaraoke.Gui/OpenKaraoke.Gui.csproj
```

Core starts and supervises its private Python worker automatically. See
[Analysis Service](analysis_service.md) for configuration and sample Job requests.
The GUI is still a bootstrap shell; its Core API client is Issue #15.
The mock adapter and the UMX-HQ stem adapter are registered. Real stem inference requires
the optional runtime described in [Stem separation](stem_separation.md). Other AI roles
remain unimplemented and return explicit errors; adapter registration is not model installation.
