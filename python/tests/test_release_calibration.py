import copy
import json
from pathlib import Path

import pytest

from open_karaoke_analysis.release_calibration import apply_thresholds, main, validate_ledger
from open_karaoke_analysis.release_evaluation import verdict

TUNING, HOLDOUT = "1" * 64, "2" * 64
LEDGER_FILE = Path(__file__).resolve().parents[2] / "release_evaluation" / "thresholds.json"


def ledger(value=None, history=()):
    return {
        "format_version": 1,
        "ledger_version": len(history),
        "thresholds": {"lyrics/cer": {"direction": "max", "value": value}},
        "history": list(history),
    }


def entry(version=1, old=None, new=0.2, tuning=TUNING, holdout=HOLDOUT):
    return {
        "version": version,
        "key": "lyrics/cer",
        "old": old,
        "new": new,
        "tuning_manifest_hash": tuning,
        "holdout_manifest_hash": holdout,
        "reason": "holdout p95 plus margin",
        "approved_by": "Tomiya",
    }


def report(value=0.1, threshold=None, status="unverified"):
    return {
        "format_version": 1,
        "app_sha": "b" * 40,
        "dataset_manifest_hash": HOLDOUT,
        "model_snapshot": "whisper-large-v3",
        "results": [
            {
                "area": "lyrics",
                "metric": "cer",
                "value": value,
                "threshold": threshold,
                "direction": "max",
                "status": status,
                "evidence_ref": "summary.json",
            }
        ],
        "blocking": ["lyrics/cer"],
    }


def test_committed_ledger_is_valid_and_uncalibrated():
    data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    validate_ledger(data)
    assert data["history"] == []
    assert all(item["value"] is None for item in data["thresholds"].values())


def test_calibrated_ledger_with_history_is_valid():
    validate_ledger(ledger(0.2, [entry()]))
    validate_ledger(ledger(0.15, [entry(), entry(2, 0.2, 0.15)]))


@pytest.mark.parametrize(
    "bad",
    [
        ledger(0.2),  # 値だけで履歴が無い
        ledger(0.2, [entry(tuning=HOLDOUT)]),  # holdoutを調整に流用
        ledger(0.2, [entry(old=0.1)]),  # oldが直前値と不一致
        ledger(0.2, [entry(version=2)]),  # 版が連番でない
        ledger(0.3, [entry()]),  # 台帳値が履歴再生と不一致
        ledger(0.2, [entry(holdout="XYZ")]),
    ],
)
def test_invalid_ledgers_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_ledger(bad)


def test_uncalibrated_ledger_keeps_value_unverified():
    applied = apply_thresholds(report(), ledger())
    assert applied["results"][0]["status"] == "unverified"
    assert verdict(applied)["releasable"] is False


def test_calibrated_threshold_is_applied_and_judged():
    calibrated = ledger(0.2, [entry()])
    passed = apply_thresholds(report(0.1), calibrated)
    assert passed["results"][0]["threshold"] == 0.2
    assert verdict(passed)["releasable"] is True
    failed = apply_thresholds(report(0.5), calibrated)
    assert verdict(failed)["failing"] == ["lyrics/cer"]


def test_report_cannot_override_ledger_threshold():
    with pytest.raises(ValueError, match="differs from the ledger"):
        apply_thresholds(report(0.1, 0.9, "pass"), ledger(0.2, [entry()]))


def test_input_is_not_mutated():
    original = report()
    snapshot = copy.deepcopy(original)
    apply_thresholds(original, ledger(0.2, [entry()]))
    assert original == snapshot


def test_cli_exit_codes(tmp_path, monkeypatch):
    led, rep = tmp_path / "t.json", tmp_path / "r.json"
    led.write_text(json.dumps(ledger(0.2, [entry()])), encoding="utf-8")
    rep.write_text(json.dumps(report(0.1)), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", str(led), str(rep)])
    assert main() == 0
    led.write_text(json.dumps(ledger()), encoding="utf-8")
    assert main() == 1
    led.write_text(json.dumps(ledger(0.2)), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
