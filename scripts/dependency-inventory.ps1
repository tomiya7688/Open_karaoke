#requires -Version 7.4
param(
    [string]$OutputDirectory = "artifacts/dependencies",
    # 同梱FFmpegディレクトリ(tools/ffmpeg)。指定時のみ ffmpeg-inventory.json を出力する。
    [string]$FfmpegDirectory
)

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
    if ($FfmpegDirectory) {
        # 成果物の実体hashとbuildconf記録を台帳化し、GPL/nonfreeが含まれていれば失敗する。
        $bin = Get-ChildItem -LiteralPath $FfmpegDirectory -Filter 'ffmpeg*' -File |
            Where-Object { $_.BaseName -eq 'ffmpeg' } | Select-Object -First 1
        $conf = Join-Path $FfmpegDirectory 'buildconf.txt'
        if (-not $bin -or -not (Test-Path -LiteralPath $conf)) {
            throw "ffmpeg binary or buildconf.txt is missing in $FfmpegDirectory"
        }
        $confText = Get-Content -LiteralPath $conf -Raw
        if ($confText -match '(?i)--enable-(gpl|nonfree)') {
            throw "ffmpeg buildconf contains --enable-gpl or --enable-nonfree"
        }
        [ordered]@{
            name = 'ffmpeg'
            file = $bin.Name
            sha256 = (Get-FileHash -LiteralPath $bin.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            buildconf_sha256 = (Get-FileHash -LiteralPath $conf -Algorithm SHA256).Hash.ToLowerInvariant()
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $out 'ffmpeg-inventory.json') -Encoding utf8
    }
}
finally {
    Pop-Location
}
