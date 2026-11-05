"""リリース判定ゲート(19.6)のテスト。実データが無い間は常にunverifiedで失敗し、合格を偽装しない。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-gate.yml"
THRESHOLDS = ROOT / "release_evaluation" / "thresholds.json"


def _run(report: Path) -> subprocess.CompletedProcess:
    """
    {
      責務: [_run: workflowと同じ呼び出しでverdict CLIを実行する]
      処理: [1: PYTHONPATH=python/src でモジュールを起動する]
      引数: [report: report.jsonのパス]
      戻り値: [CompletedProcess(終了コードと標準出力)]
    }
    """
    env = {"PYTHONPATH": str(ROOT / "python" / "src"), "SYSTEMROOT": "C:\\Windows"}
    return subprocess.run(
        [sys.executable, "-m", "open_karaoke_analysis.release_evaluation", str(report)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _uncalibrated_report() -> dict:
    """
    {
      責務: [_uncalibrated_report: 台帳の全項目をblockingにし結果なしの報告を作る]
      処理: [1: thresholds.jsonのキーをblockingへ, 2: resultsは空にする]
      戻り値: [report.jsonの内容]
    }
    """
    ledger = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    return {
        "format_version": 1,
        "app_sha": "b" * 40,
        "dataset_manifest_hash": "a" * 64,
        "model_snapshot": "none",
        "results": [],
        "blocking": sorted(ledger["thresholds"]),
    }


def test_ledger_is_uncalibrated_so_gate_cannot_pass_today():
    ledger = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    values = {key: entry["value"] for key, entry in ledger["thresholds"].items()}
    assert values and all(value is None for value in values.values())


def test_uncalibrated_report_fails_gate_with_all_unverified(tmp_path):
    report = _uncalibrated_report()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    done = _run(path)
    assert done.returncode == 1
    verdict = json.loads(done.stdout)
    assert verdict["releasable"] is False
    assert verdict["unverified"] == report["blocking"]
    assert verdict["failing"] == []


def test_forged_pass_without_calibrated_threshold_is_rejected(tmp_path):
    report = _uncalibrated_report()
    report["results"] = [
        {
            "area": "lyrics",
            "metric": "cer",
            "value": 0.0,
            "threshold": None,
            "direction": "max",
            "status": "pass",
            "evidence_ref": "forged.json",
        }
    ]
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert _run(path).returncode == 2


@pytest.mark.parametrize("content", ["{}", "not json"])
def test_invalid_or_missing_report_fails_gate(tmp_path, content):
    path = tmp_path / "report.json"
    path.write_text(content, encoding="utf-8")
    assert _run(path).returncode == 2
    assert _run(tmp_path / "missing.json").returncode == 2


def test_no_committed_report_exists_so_default_gate_run_fails():
    assert not (ROOT / "release_evaluation" / "1.0.0" / "report.json").exists()


def test_workflow_is_manual_only_and_never_masks_failure():
    text = WORKFLOW.read_text(encoding="utf-8")
    on_block = text.split("\njobs:")[0]
    assert "workflow_dispatch:" in on_block
    for trigger in ("push:", "pull_request:", "schedule:", "workflow_run:"):
        assert trigger not in on_block
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "python -m open_karaoke_analysis.release_evaluation" in text
    assert 'exit "$code"' in text
    assert "if [ ! -f" in text and "exit 1" in text
    assert "contents: read" in text
