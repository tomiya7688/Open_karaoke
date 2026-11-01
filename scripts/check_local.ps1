#requires -Version 7.4
<#
.SYNOPSIS
PR前のローカル簡易自己チェック。単一の公開入口。

.DESCRIPTION
変更ファイル（BaseとのcommitDiff + 未commit + 未追跡）から必要な検査を自動選択し、
PASS / FAIL / SKIP / UNVERIFIED を1つの要約で表示する。
読み取り専用・ネットワーク不要。依存の導入・自動修正・git add/reset は行わない。
ログは artifacts/check_local/ にだけ書く。完全な検査は CI と scripts/check.ps1。

終了コード: 0 = 必須検査がすべてPASS、1 = FAILあり、2 = 検査対象0件（PASS扱いにしない）。

.EXAMPLE
pwsh scripts/check_local.ps1
#>
[CmdletBinding()]
param(
    # 比較元ref。fetchはしない（最新化は利用者が事前に git fetch する）。
    [string]$Base = 'origin/main',
    # 変更ファイルではなく全追跡ファイルを対象にする。
    [switch]$All,
    # 検査を実行せず、選択結果だけを表示する。
    [switch]$PlanOnly,
    # 自動検出の代わりに使う変更ファイル（repo相対）。主に自己テスト用。
    [string[]]$ChangedFiles,
    # 検査選択と失敗検出の自己テストを実行する。
    [switch]$SelfTest,
    # Python実行ファイル。未指定なら OPEN_KARAOKE_DEV_PYTHON → repo/.venv → メインcheckoutの.venv → PATH。
    [string]$Python
)

$ErrorActionPreference = 'Stop'
# 終了コードは各呼出しで判定する。利用者のプロファイル設定に依存させない。
$PSNativeCommandUseErrorActionPreference = $false
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo 'artifacts/check_local'

# Pythonモジュール名の先頭語 → テスト接頭辞。docs/ai_workflow.md の Task routes と対応する。
$pythonTestAlias = @{
    whisper = 'lyric'
    ctc     = 'alignment'
    notes   = 'note'
    lyrics  = 'lyric'
    stems   = 'stem'
}
# 変更されたら Python 全体を検査する共有契約・パッケージ設定。
$pythonSharedFiles = @(
    'python/pyproject.toml',
    'python/src/open_karaoke_analysis/__init__.py',
    'python/src/open_karaoke_analysis/__main__.py',
    'python/src/open_karaoke_analysis/contracts.py',
    'python/src/open_karaoke_analysis/adapters.py',
    'python/src/open_karaoke_analysis/service.py'
)

function New-Check {
    param([string]$Id, [string]$Title, [bool]$Required, [scriptblock]$Run, [string]$Rerun, $Data, [string]$After)
    # After: このIdの検査がPASSしなかったら実行せずFAILにする（前提のbuildが無い状態での誤PASS防止）。
    [pscustomobject]@{ Id = $Id; Title = $Title; Required = $Required; Run = $Run; Rerun = $Rerun; Data = $Data; After = $After }
}

function Get-ChangedFileSet {
    param([string]$BaseRef)
    $null = git rev-parse --verify --quiet "$BaseRef^{commit}"
    if ($LASTEXITCODE -ne 0) {
        throw "比較元 '$BaseRef' が見つかりません。'git fetch origin' 後に再実行するか -Base を指定してください。"
    }
    $mergeBase = git merge-base HEAD $BaseRef
    $committed = @(git -c core.quotepath=false diff --name-only "$mergeBase" HEAD)
    $worktree = @(git -c core.quotepath=false diff --name-only HEAD)
    $untracked = @(git -c core.quotepath=false ls-files --others --exclude-standard)
    [pscustomobject]@{
        MergeBase = $mergeBase
        Files     = @($committed + $worktree + $untracked | Where-Object { $_ } | Sort-Object -Unique)
        Untracked = $untracked
    }
}

function Get-PythonTestFile {
    param([string[]]$Files, [string]$TestRoot)
    $tests = [System.Collections.Generic.SortedSet[string]]::new()
    $needAll = $false
    foreach ($file in $Files) {
        if ($pythonSharedFiles -contains $file) { $needAll = $true; continue }
        if ($file -match '^python/tests/(test_[^/]+\.py)$') { [void]$tests.Add($file); continue }
        if ($file -eq 'scripts/check_python_trust.py') { [void]$tests.Add('python/tests/test_python_trust.py'); continue }
        if ($file -match '^python/src/open_karaoke_analysis/([^/]+)\.py$') {
            $head = ($Matches[1] -split '_')[0]
            if ($pythonTestAlias.ContainsKey($head)) { $head = $pythonTestAlias[$head] }
            $found = @(Get-ChildItem -Path $TestRoot -Filter "test_$head*.py" -File -ErrorAction SilentlyContinue)
            if ($found.Count -eq 0) { $needAll = $true; continue }
            foreach ($f in $found) { [void]$tests.Add("python/tests/$($f.Name)") }
            continue
        }
        # fixture・conftest・その他のPython配下は影響範囲を特定しないので全体。
        if ($file -like 'python/*') { $needAll = $true }
    }
    if ($needAll) { return @('python/tests') }
    @($tests | Where-Object { Test-Path (Join-Path $repo $_) })
}

function Get-CheckPlan {
    param([string[]]$Files, [string[]]$Untracked, [string]$MergeBase, [bool]$Everything)
    $plan = [System.Collections.Generic.List[object]]::new()
    $covered = [System.Collections.Generic.HashSet[string]]::new()
    $existing = @($Files | Where-Object { Test-Path -LiteralPath (Join-Path $repo $_) -PathType Leaf })

    $plan.Add((New-Check 'diff-check' 'git diff --check（空白・競合マーカー）' $true { param($d)
                $out = @()
                if ($d.MergeBase) { $out += @(git diff --check $d.MergeBase 2>&1) }
                foreach ($u in $d.Untracked) {
                    $out += @(git diff --no-index --check -- /dev/null $u 2>&1 | Where-Object { $_ -notmatch '^warning:' })
                }
                $out = @($out | Where-Object { $_ })
                if ($out.Count) { $out; throw '空白エラーまたは競合マーカーがあります' }
            } 'git diff --check' @{ MergeBase = $MergeBase; Untracked = $Untracked }))

    $py = @($existing | Where-Object { $_ -like '*.py' })
    if ($py.Count) {
        $py | ForEach-Object { [void]$covered.Add($_) }
        $plan.Add((New-Check 'py-lint' "ruff check（$($py.Count)ファイル）" $true { param($d)
                    Invoke-Python (@('-m', 'ruff', 'check') + $d)
                } 'python -m ruff check <files>' $py))
        $plan.Add((New-Check 'py-format' "ruff format --check（$($py.Count)ファイル）" $true { param($d)
                    Invoke-Python (@('-m', 'ruff', 'format', '--check') + $d)
                } 'python -m ruff format --check <files>' $py))
    }
    $pyTests = @(Get-PythonTestFile -Files $(if ($Everything) { @('python/pyproject.toml') } else { $Files }) -TestRoot (Join-Path $repo 'python/tests'))
    if ($pyTests.Count) {
        $Files | Where-Object { $_ -like 'python/*' } | ForEach-Object { [void]$covered.Add($_) }
        $plan.Add((New-Check 'py-test' "pytest（$($pyTests -join ', ')）" $true { param($d)
                    Invoke-Python (@('-m', 'pytest', '-q', '-p', 'no:cacheprovider', "--basetemp=$logDir/pytest-tmp") + $d)
                } "python -m pytest -q $($pyTests -join ' ')" $pyTests))
    }

    $rust = @($Files | Where-Object { $_ -like 'core/*' -or $_ -in @('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml') })
    if ($rust.Count -or $Everything) {
        $rust | ForEach-Object { [void]$covered.Add($_) }
        $plan.Add((New-Check 'rust-fmt' 'cargo fmt --check' $true { param($d) Invoke-Tool cargo @('fmt', '--all', '--', '--check') } 'cargo fmt --all -- --check'))
        $plan.Add((New-Check 'rust-clippy' 'cargo clippy -D warnings（offline）' $true { param($d)
                    Invoke-Tool cargo @('clippy', '--locked', '--offline', '--workspace', '--all-targets', '--', '-D', 'warnings')
                } 'cargo clippy --locked --offline --workspace --all-targets -- -D warnings'))
        $plan.Add((New-Check 'rust-test' 'cargo test（offline、ignored除く）' $true { param($d)
                    Invoke-Tool cargo @('test', '--locked', '--offline', '--workspace', '--all-targets')
                } 'cargo test --locked --offline --workspace --all-targets'))
    }

    $gui = @($Files | Where-Object { $_ -like 'gui/*' })
    if ($gui.Count -or $Everything) {
        $gui | ForEach-Object { [void]$covered.Add($_) }
        # slnはGUI本体とClientだけを含む。slnの外のTests/Smokeは変更時だけ個別に整形検査する。
        # Client.Testsは --unit（Core/Python不要）の実行のため常にbuildする。
        $formatTargets = @('gui/OpenKaraoke.sln') + @($gui | ForEach-Object { if ($_ -match '^gui/(OpenKaraoke\.(Client\.Tests|Gui\.Smoke))/') { "gui/$($Matches[1])" } } | Sort-Object -Unique)
        $targets = @($formatTargets + 'gui/OpenKaraoke.Client.Tests' | Sort-Object -Unique)
        $plan.Add((New-Check 'gui-build' "dotnet build --no-restore（$($targets -join ', ')）" $true { param($d)
                    foreach ($target in $d) {
                        try { Invoke-Tool dotnet @('build', $target, '--configuration', 'Release', '--no-restore', '-nologo', '-v:q') }
                        catch {
                            $_.Exception.Message
                            throw "$target のbuildに失敗（NETSDK1004なら未restore: 'dotnet restore $target' を1回実行。この検査は自動restoreしません）"
                        }
                    }
                } 'dotnet build <target> --configuration Release --no-restore' $targets))
        $plan.Add((New-Check 'gui-format' 'dotnet format --verify-no-changes' $true { param($d)
                    foreach ($target in $d) { Invoke-Tool dotnet @('format', $target, '--verify-no-changes', '--no-restore') }
                } 'dotnet format <target> --verify-no-changes --no-restore' $formatTargets 'gui-build'))
        $plan.Add((New-Check 'gui-unit' 'OpenKaraoke.Client.Tests --unit' $true { param($d)
                    Invoke-Tool dotnet @('run', '--project', 'gui/OpenKaraoke.Client.Tests', '--configuration', 'Release', '--no-build', '--', '--unit')
                } 'dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- --unit' $null 'gui-build'))
    }
    $ps1 = @($existing | Where-Object { $_ -like '*.ps1' })
    if ($ps1.Count) {
        $ps1 | ForEach-Object { [void]$covered.Add($_) }
        $plan.Add((New-Check 'ps-syntax' "PowerShell構文（$($ps1.Count)ファイル）" $true { param($d)
                    $bad = foreach ($f in $d) {
                        $errors = $null
                        $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo $f), [ref]$null, [ref]$errors)
                        foreach ($e in $errors) { "${f}:$($e.Extent.StartLineNumber): $($e.Message)" }
                    }
                    if ($bad) { $bad; throw 'PowerShellの構文エラーがあります' }
                } 'pwsh scripts/check_local.ps1' $ps1))
    }

    $md = @($existing | Where-Object { $_ -like '*.md' })
    if ($md.Count) {
        $md | ForEach-Object { [void]$covered.Add($_) }
        $plan.Add((New-Check 'md-links' "Markdown相対リンク（$($md.Count)ファイル）" $true { param($d)
                    $bad = foreach ($f in $d) {
                        $dir = Split-Path -Parent (Join-Path $repo $f)
                        $text = Get-Content -LiteralPath (Join-Path $repo $f) -Raw
                        foreach ($m in [regex]::Matches($text, '\]\(([^)\s]+)\)')) {
                            $target = ($m.Groups[1].Value -split '#')[0]
                            if (-not $target -or $target -match '^[a-zA-Z][a-zA-Z0-9+.-]*:') { continue }
                            $target = [uri]::UnescapeDataString($target)
                            if (-not (Test-Path -LiteralPath (Join-Path $dir $target))) { "${f}: リンク先なし $target" }
                        }
                    }
                    if ($bad) { $bad; throw 'Markdownのリンク切れがあります' }
                } 'pwsh scripts/check_local.ps1' $md))
    }

    # 自動選択の対象外。ローカルでは確認しておらず、成功とは表示しない。
    $rest = @($Files | Where-Object { -not $covered.Contains($_) })
    $workflows = @($rest | Where-Object { $_ -like '.github/*' })
    if ($workflows.Count) {
        $plan.Add((New-Check 'ci-config' "CI設定（$($workflows -join ', ')）" $false { param($d)
                    throw 'UNVERIFIED: workflowの挙動はCIでのみ確認できます'
                } 'PRのCI結果を確認'))
    }
    $other = @($rest | Where-Object { $_ -notlike '.github/*' })
    if ($other.Count) {
        $plan.Add((New-Check 'uncovered' "自動検査なし（$($other -join ', ')）" $false { param($d)
                    throw 'UNVERIFIED: 対応する軽量検査がありません。差分を目視で確認してください'
                } '目視確認・CI'))
    }
    $plan
}

function Resolve-Python {
    if ($Python) { return $Python }
    if ($env:OPEN_KARAOKE_DEV_PYTHON) { return $env:OPEN_KARAOKE_DEV_PYTHON }
    $roots = @($repo)
    $common = git -C $repo rev-parse --path-format=absolute --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $common) { $roots += (Split-Path -Parent $common) }
    foreach ($root in $roots) {
        foreach ($rel in '.venv/Scripts/python.exe', '.venv/bin/python') {
            $candidate = Join-Path $root $rel
            if (Test-Path $candidate) { return $candidate }
        }
    }
    'python'
}

function Invoke-Tool {
    # 引数は配列で受ける。関数呼出しでは裸の '--' がPowerShellに消費されるため。
    param([string]$exe, [string[]]$rest)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { throw "依存不足: '$exe' が見つかりません" }
    & $exe @rest 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$exe が終了コード $LASTEXITCODE で失敗しました" }
}

function Invoke-Python {
    param([string[]]$ToolArgs)
    # worktreeでも作業中のsourceを読み込むよう、python/src を先頭に置く。
    $previous = $env:PYTHONPATH
    $env:PYTHONPATH = (@((Join-Path $repo 'python/src'), $previous) | Where-Object { $_ }) -join [IO.Path]::PathSeparator
    try { Invoke-Tool $script:pythonExe $ToolArgs }
    finally { $env:PYTHONPATH = $previous }
}

function Test-PythonTooling {
    $null = & $script:pythonExe -c 'import ruff, pytest, httpx, numpy, scipy, soundfile' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "依存不足: '$script:pythonExe' に開発依存（python[dev]: ruff/pytest/httpx/numpy/scipy/soundfile）がありません。'python -m pip install -e ""./python[dev]""' を実行するか -Python / OPEN_KARAOKE_DEV_PYTHON で開発用venvを指定してください"
    }
}

function Invoke-Plan {
    param([object[]]$Plan)
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $done = @{}
    $results = foreach ($check in $Plan) {
        $log = Join-Path $logDir "$($check.Id).log"
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $status = 'PASS'; $reason = ''
        Set-Content -LiteralPath $log -Value '' -Encoding utf8
        try {
            if ($check.After -and $done[$check.After] -ne 'PASS') { throw "前提の検査 '$($check.After)' がPASSしていないため未実行" }
            if ($check.Id -like 'py-*') { Test-PythonTooling }
            & $check.Run $check.Data *>&1 | Out-File -LiteralPath $log -Encoding utf8
        }
        catch {
            $reason = $_.Exception.Message
            if (Select-String -LiteralPath $log -Pattern 'ModuleNotFoundError|No module named' -Quiet) { $reason = "依存不足（ログにModuleNotFoundError）: $reason" }
            Add-Content -LiteralPath $log -Value $reason -Encoding utf8
            $status = if ($reason -like 'UNVERIFIED:*') { 'UNVERIFIED' } elseif ($check.Required) { 'FAIL' } else { 'SKIP' }
            $reason = $reason -replace '^UNVERIFIED:\s*', ''
        }
        $watch.Stop()
        $done[$check.Id] = $status
        if ($status -eq 'FAIL') {
            Write-Host "---- $($check.Id) の末尾ログ ($log)" -ForegroundColor Red
            Get-Content -LiteralPath $log -Tail 30 | Write-Host
        }
        [pscustomobject]@{
            Status = $status; Check = $check.Title; Seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1)
            Reason = $reason; Rerun = $check.Rerun
        }
    }
    $results
}

function Write-Summary {
    param([object[]]$Results)
    Write-Host ''
    Write-Host '==== check_local 要約 ====' -ForegroundColor Cyan
    foreach ($r in $Results) {
        $color = @{ PASS = 'Green'; FAIL = 'Red'; SKIP = 'Yellow'; UNVERIFIED = 'Yellow' }[$r.Status]
        Write-Host ('{0,-10} {1} ({2}s)' -f $r.Status, $r.Check, $r.Seconds) -ForegroundColor $color
        if ($r.Status -ne 'PASS') {
            Write-Host "           理由: $($r.Reason)"
            Write-Host "           再実行: $($r.Rerun)"
        }
    }
    $open = @($Results | Where-Object Status -In 'SKIP', 'UNVERIFIED').Count
    if ($Results.Status -contains 'FAIL') { Write-Host '結果: FAIL' -ForegroundColor Red }
    elseif ($open) { Write-Host "結果: 必須検査はPASS。SKIP/UNVERIFIED $open 件は未確認として PR の Unverified に書く" -ForegroundColor Yellow }
    else { Write-Host '結果: PASS' -ForegroundColor Green }
    Write-Host 'ローカル対象外（CIで確認）: job_apiの--ignored実プロセス試験、Client.Tests/Gui.Smokeの実Core結合試験、実モデル、GUI目視、Linux/複数OS、配布物。'
}

function Invoke-SelfTest {
    $failures = [System.Collections.Generic.List[string]]::new()
    function Assert-Plan([string[]]$files, [string[]]$expectIds, [string[]]$rejectIds) {
        $ids = @(Get-CheckPlan -Files $files -Untracked @() -MergeBase '' -Everything $false | ForEach-Object Id)
        foreach ($id in $expectIds) { if ($ids -notcontains $id) { $failures.Add("[$($files -join ',')] に $id がない") } }
        foreach ($id in $rejectIds) { if ($ids -contains $id) { $failures.Add("[$($files -join ',')] に不要な $id がある") } }
    }
    Assert-Plan @('core/src/lib.rs') @('diff-check', 'rust-fmt', 'rust-clippy', 'rust-test') @('py-test', 'gui-build', 'uncovered')
    Assert-Plan @('python/src/open_karaoke_analysis/pitch_fusion.py') @('py-lint', 'py-format', 'py-test') @('rust-test')
    Assert-Plan @('gui/OpenKaraoke.Gui/App.axaml') @('gui-build', 'gui-format', 'gui-unit') @('py-test')
    Assert-Plan @('.github/workflows/ci.yml') @('ci-config') @('rust-test')
    Assert-Plan @('LICENSE') @('uncovered') @()
    $pitchTests = @(Get-PythonTestFile -Files @('python/src/open_karaoke_analysis/pitch_fusion.py') -TestRoot (Join-Path $repo 'python/tests'))
    if ($pitchTests -notcontains 'python/tests/test_pitch.py' -or $pitchTests -contains 'python/tests') {
        $failures.Add("pitch_fusion の対象テストが不正: $($pitchTests -join ',')")
    }
    $shared = @(Get-PythonTestFile -Files @('python/src/open_karaoke_analysis/contracts.py') -TestRoot (Join-Path $repo 'python/tests'))
    if ($shared -join ',' -ne 'python/tests') { $failures.Add("共有契約の変更でPython全体にならない: $($shared -join ',')") }

    # 失敗注入: 構文エラー・失敗テスト・リンク切れが FAIL になること。
    $fixture = Join-Path $logDir 'selftest'
    Remove-Item -Recurse -Force $fixture -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $fixture | Out-Null
    Set-Content (Join-Path $fixture 'bad.ps1') 'function Broken {' -Encoding utf8
    Set-Content (Join-Path $fixture 'test_selftest_fail.py') "def test_fails():`n    assert 1 == 2`n" -Encoding utf8
    Set-Content (Join-Path $fixture 'bad.md') '[x](missing-file.md)' -Encoding utf8
    $rel = [IO.Path]::GetRelativePath($repo, $fixture) -replace '\\', '/'
    $injected = @(
        (New-Check 'ps-syntax' 'inject ps' $true { param($d)
                $errors = $null
                $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $d 'bad.ps1'), [ref]$null, [ref]$errors)
                if ($errors) { throw 'syntax' }
            } '' $fixture),
        (New-Check 'py-test' 'inject pytest' $true { param($d)
                Invoke-Python @('-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--rootdir', $d[0], "$($d[1])/test_selftest_fail.py")
            } '' @($fixture, $rel))
    )
    $injected += @(Get-CheckPlan -Files @("$rel/bad.md") -Untracked @() -MergeBase '' -Everything $false | Where-Object Id -EQ 'md-links')
    $res = @(Invoke-Plan $injected)
    foreach ($r in $res) { if ($r.Status -ne 'FAIL') { $failures.Add("失敗注入が検出されない: $($r.Check) => $($r.Status)") } }
    Remove-Item -Recurse -Force $fixture -ErrorAction SilentlyContinue

    if ($failures.Count) { $failures | ForEach-Object { Write-Host "SELFTEST FAIL: $_" -ForegroundColor Red }; exit 1 }
    Write-Host 'SELFTEST PASS: 検査選択と失敗注入（構文エラー・失敗テスト・リンク切れ）' -ForegroundColor Green
    exit 0
}

Push-Location $repo
try {
    $script:pythonExe = Resolve-Python
    if ($SelfTest) { Invoke-SelfTest }

    if ($ChangedFiles) {
        $set = [pscustomobject]@{ MergeBase = ''; Files = @($ChangedFiles -split ',' | ForEach-Object { $_.Trim() -replace '\\', '/' } | Where-Object { $_ }); Untracked = @() }
    }
    elseif ($All) {
        $set = [pscustomobject]@{ MergeBase = ''; Files = @(git -c core.quotepath=false ls-files); Untracked = @() }
    }
    else {
        $set = Get-ChangedFileSet -BaseRef $Base
    }

    if ($set.Files.Count -eq 0) {
        Write-Host "検査対象0件: '$Base' からの変更がありません。PASSではありません（全体は -All）。" -ForegroundColor Yellow
        exit 2
    }

    Write-Host "変更ファイル $($set.Files.Count) 件（base: $(if ($set.MergeBase) { $set.MergeBase.Substring(0, 12) } else { 'n/a' })）、Python: $script:pythonExe"
    $plan = @(Get-CheckPlan -Files $set.Files -Untracked $set.Untracked -MergeBase $set.MergeBase -Everything $All.IsPresent)
    if ($PlanOnly) { $plan | ForEach-Object { '{0,-12} {1}' -f $_.Id, $_.Title }; exit 0 }

    $results = @(Invoke-Plan $plan)
    Write-Summary $results
    if ($results.Status -contains 'FAIL') { exit 1 }
    exit 0
}
finally {
    Pop-Location
}
