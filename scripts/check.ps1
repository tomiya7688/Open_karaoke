#requires -Version 7.4
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$repo = Split-Path -Parent $PSScriptRoot

Push-Location $repo
try {
    python -m pip install -e "./python[dev]"
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo test --workspace --all-targets
    cargo test --test job_api -- --ignored
    python -m ruff check python
    python -m ruff format --check python
    python -m pytest python/tests -q
    dotnet restore gui/OpenKaraoke.sln
    dotnet build gui/OpenKaraoke.sln --configuration Release --no-restore
    dotnet format gui/OpenKaraoke.sln --verify-no-changes --no-restore
}
finally {
    Pop-Location
}
