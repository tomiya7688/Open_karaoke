#requires -Version 7.4
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$repo = Split-Path -Parent $PSScriptRoot

Push-Location $repo
try {
    cargo build --workspace --all-targets
    python -m pip install --upgrade "pip>=26.2.2" "certifi>=2026.10.19"
    python scripts/check_python_trust.py
    python -m pip install -e "./python[dev]"
    python -m compileall -q python/src
    dotnet restore gui/OpenKaraoke.sln
    dotnet build gui/OpenKaraoke.sln --configuration Release --no-restore
}
finally {
    Pop-Location
}
