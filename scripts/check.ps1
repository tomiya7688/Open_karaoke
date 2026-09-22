$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Push-Location $repo
try {
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings
    cargo test --workspace --all-targets
    python -m pip install -e "./python[dev]"
    python -m ruff check python
    python -m compileall -q python/src
    dotnet restore gui/OpenKaraoke.sln
    dotnet build gui/OpenKaraoke.sln --configuration Release --no-restore
    dotnet format gui/OpenKaraoke.sln --verify-no-changes --no-restore
}
finally {
    Pop-Location
}
