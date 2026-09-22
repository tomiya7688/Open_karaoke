# Development

## Prerequisites

The repository is a monorepo with three components:

- Rust stable toolchain with rustfmt and clippy
- Python 3.11 or newer
- .NET 8 SDK

The production distribution must bundle or manage required runtimes and tools so end users are not asked to install this development stack.

## Layout

- `core/`: Rust product core
- `python/`: offline analysis service
- `gui/`: C# / Avalonia desktop GUI
- `scripts/`: developer and CI entry points

## Build

```powershell
./scripts/build.ps1
```

## Static checks and tests

```powershell
./scripts/check.ps1
```

## Dependency inventory

```powershell
./scripts/dependency-inventory.ps1
```

CI uploads the generated `artifacts/dependencies/` directory so dependency and license review has a reproducible input.

## Running bootstrap components

```powershell
cargo run -p open-karaoke-core
python -m open_karaoke_analysis
dotnet run --project gui/OpenKaraoke.Gui/OpenKaraoke.Gui.csproj
```

These are bootstrap executables only. Product APIs and audio/analysis features are implemented by later issues.
