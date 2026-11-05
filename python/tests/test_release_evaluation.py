import copy
import json

import pytest

from open_karaoke_analysis.evaluation_summary import lyric_summary
from open_karaoke_analysis.lyric_evaluator import evaluate
from open_karaoke_analysis.release_evaluation import lyric_result, main, validate_report, verdict

SHA = "a" * 64


def row(metric="cer", value=0.1, threshold=0.2, status="pass", area="lyrics", direction="max"):
    return {
        "area": area,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "direction": direction,
        "status": status,
        "evidence_ref": "summary.json" if value is not None else None,
    }


def base(results=None, blocking=("lyrics/cer",)):
    return {
        "format_version": 1,
        "app_sha": "b" * 40,
        "dataset_manifest_hash": SHA,
        "model_snapshot": "whisper-large-v3",
        "results": [row()] if results is None else results,
        "blocking": list(blocking),
    }


def test_all_blocking_pass_is_releasable():
    result = verdict(base())
    assert result["releasable"] is True
    assert result["blocking_status"] == {"lyrics/cer": "pass"}


def test_fail_blocks_release():
    result = verdict(base([row(value=0.5, status="fail")]))
    assert result["releasable"] is False
    assert result["failing"] == ["lyrics/cer"]


def test_unverified_and_missing_blocking_block_release():
    unverified = verdict(base([row(value=None, threshold=None, status="unverified")]))
    assert unverified["releasable"] is False and unverified["unverified"] == ["lyrics/cer"]
    missing = verdict(base([], blocking=("lyrics/cer",)))
    assert missing["releasable"] is False and missing["unverified"] == ["lyrics/cer"]


def test_non_blocking_failure_does_not_block():
    report = base([row(), row("wer", 0.9, 0.2, "fail")])
    assert verdict(report)["releasable"] is True


def test_blocking_pass_without_threshold_is_rejected():
    with pytest.raises(ValueError, match="calibrated threshold"):
        validate_report(base([row(threshold=None)]))


def test_pass_contradicting_threshold_or_lacking_evidence_is_rejected():
    with pytest.raises(ValueError, match="contradicts"):
        validate_report(base([row(value=0.3)]))
    with pytest.raises(ValueError, match="contradicts"):
        validate_report(base([row("note_f1", 0.5, 0.8, area="notes", direction="min")]))
    bad = row()
    bad["evidence_ref"] = None
    with pytest.raises(ValueError, match="evidence_ref"):
        validate_report(base([bad]))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(extra=1),
        lambda r: r.update(app_sha="short"),
        lambda r: r.update(dataset_manifest_hash="A" * 64),
        lambda r: r.update(blocking=[]),
        lambda r: r.update(blocking=["lyrics/cer", "lyrics/cer"]),
        lambda r: r["results"][0].update(status="skipped"),
        lambda r: r["results"][0].update(value=float("nan")),
        lambda r: r["results"][0].update(value=True),
        lambda r: r["results"][0].update(song_title="x"),
        lambda r: r["results"].append(row()),
    ],
)
def test_invalid_reports_are_rejected(mutate):
    report = copy.deepcopy(base())
    mutate(report)
    with pytest.raises(ValueError):
        validate_report(report)


def test_lyric_result_reuses_evaluation_summary():
    meta = {
        "dataset_id": "d",
        "dataset_version": "1",
        "dataset_sha256": SHA,
        "model_name": "whisper",
        "model_version": "v3",
    }
    data = [{"reference": "青い空", "hypothesis": "青い空"}]
    real = lyric_summary(evaluate(data), evaluation_kind="real_singing", **meta)
    assert lyric_result(real, evidence_ref="s.json", threshold=0.2)["status"] == "pass"
    assert lyric_result(real, evidence_ref="s.json", threshold=None)["status"] == "unverified"
    wrong = evaluate([{"reference": "青い空", "hypothesis": "赤い海"}])
    bad = lyric_summary(wrong, evaluation_kind="real_singing", **meta)
    assert lyric_result(bad, evidence_ref="s.json", threshold=0.2)["status"] == "fail"
    synthetic = lyric_summary(evaluate(data), evaluation_kind="synthetic", **meta)
    with pytest.raises(ValueError, match="real_singing"):
        lyric_result(synthetic, evidence_ref="s.json", threshold=0.2)


def test_cli_exit_codes(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(base()), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", str(path)])
    assert main() == 0
    unverified = base([row(value=None, threshold=None, status="unverified")])
    path.write_text(json.dumps(unverified), encoding="utf-8")
    assert main() == 1
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
