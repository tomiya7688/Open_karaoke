$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Push-Location $repo
try {
    cargo build --workspace --all-targets
    python -m pip install -e "./python[dev]"
    python -m compileall -q python/src
    dotnet restore gui/OpenKaraoke.sln
    dotnet build gui/OpenKaraoke.sln --configuration Release --no-restore
}
finally {
    Pop-Location
}
