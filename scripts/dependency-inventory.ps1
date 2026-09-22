#requires -Version 7.4
param([string]$OutputDirectory = "artifacts/dependencies")

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $repo $OutputDirectory
New-Item -ItemType Directory -Force -Path $out | Out-Null

Push-Location $repo
try {
    cargo metadata --format-version 1 > (Join-Path $out "cargo-metadata.json")
    python -m pip freeze > (Join-Path $out "python-freeze.txt")
    dotnet list gui/OpenKaraoke.sln package --include-transitive > (Join-Path $out "dotnet-packages.txt")
}
finally {
    Pop-Location
}
