import json
import sys

import pytest

from open_karaoke_analysis.evaluation_summary import lyric_summary
from open_karaoke_analysis.lyric_evaluator import evaluate, main

SHA = "a" * 64
META = {
    "dataset_id": "private-singing",
    "dataset_version": "2026.1",
    "dataset_sha256": SHA,
    "model_name": "whisper",
    "model_version": "large-v3",
    "evaluation_kind": "real_singing",
}


def report(**kwargs):
    return evaluate([{"reference": "青い空", "hypothesis": "青い海"}], **kwargs)


def test_summary_is_aggregate_only_and_versioned():
    summary = lyric_summary(report(), **META)
    assert summary["format_version"] == 1
    assert summary["evaluation_kind"] == "real_singing"
    assert summary["evaluator"] == {"name": "lyric-transcription", "version": 1}
    assert summary["dataset"] == {"id": "private-singing", "version": "2026.1", "sha256": SHA}
    assert summary["model"] == {"name": "whisper", "version": "large-v3"}
    assert summary["metrics"]["cer"] == pytest.approx(1 / 3)
    assert summary["metrics"]["reference_chars"] == 3
    assert summary["gate"] == {"applied": False, "passed": None, "thresholds": {"max_cer": None}}
    assert "青い空" not in json.dumps(summary, ensure_ascii=False)


def test_quality_gate_result_is_carried_over():
    failed = lyric_summary(report(max_cer=0.1), **META)
    assert failed["gate"] == {"applied": True, "passed": False, "thresholds": {"max_cer": 0.1}}
    passed = lyric_summary(report(max_cer=0.5), **META)
    assert passed["gate"]["passed"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset_id", " "),
        ("dataset_version", "x" * 129),
        ("model_name", ""),
        ("model_version", None),
        ("dataset_sha256", "A" * 64),
        ("dataset_sha256", "a" * 63),
        ("evaluation_kind", "production"),
    ],
)
def test_invalid_metadata_is_rejected(field, value):
    with pytest.raises(ValueError):
        lyric_summary(report(), **{**META, field: value})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(evaluator="other"),
        lambda r: r.update(version=2),
        lambda r: r.pop("cer"),
        lambda r: r.update(cer=float("nan")),
        lambda r: r.update(wer=True),
        lambda r: r.update(passed=True),
        lambda r: r.update(quality_gate_applied="yes"),
        lambda r: r.update(max_cer=0.3),
        lambda r: r["totals"].update(char_errors=-1),
        lambda r: r.update(empty_reference_insertions=-1),
        lambda r: r.update(normalization="other"),
        lambda r: r.update(wer_tokenizer="morphology"),
        lambda r: r.update(cer=0.99),
        lambda r: r.update(wer=0.0),
        lambda r: r.update(cer=None),
        lambda r: r["totals"].update(reference_chars=r["totals"]["reference_chars"] + 1),
    ],
)
def test_inconsistent_reports_are_rejected(mutate):
    broken = report()
    mutate(broken)
    with pytest.raises(ValueError):
        lyric_summary(broken, **META)


@pytest.mark.parametrize(("max_cer", "passed"), [(0.1, False), (0.5, True)])
def test_gate_result_must_match_the_metrics(max_cer, passed):
    honest = report(max_cer=max_cer)
    assert honest["passed"] is passed
    assert lyric_summary(honest, **META)["gate"]["passed"] is passed
    forged = report(max_cer=max_cer)
    forged["passed"] = not passed
    with pytest.raises(ValueError, match="inconsistent with the lyric metrics"):
        lyric_summary(forged, **META)


def test_a_gate_fails_when_an_empty_reference_received_insertions():
    honest = evaluate(
        [
            {"id": "a", "reference": "青い空", "hypothesis": "青い空"},
            {"id": "b", "reference": "", "hypothesis": "あ"},
        ],
        max_cer=1.0,
    )
    assert honest["passed"] is False and honest["empty_reference_insertions"] == 1
    assert lyric_summary(honest, **META)["gate"]["passed"] is False
    honest["passed"] = True
    with pytest.raises(ValueError, match="inconsistent with the lyric metrics"):
        lyric_summary(honest, **META)


@pytest.mark.parametrize(
    ("pairs", "cer", "wer"),
    [
        # 分母0で誤りも0 -> 0.0
        ([{"reference": "", "hypothesis": ""}], 0.0, 0.0),
        # 分母0で誤りがある(空の参照への挿入) -> null
        ([{"reference": "", "hypothesis": "あ"}], None, None),
    ],
)
def test_rates_with_an_empty_reference_follow_the_evaluator_definition(pairs, cer, wer):
    summary = lyric_summary(evaluate(pairs), **META)
    assert summary["metrics"]["cer"] == cer and summary["metrics"]["wer"] == wer
    for rate, expected in (("cer", cer), ("wer", wer)):
        broken = evaluate(pairs)
        broken[rate] = 1.0 if expected is None else None
        with pytest.raises(ValueError, match=f"{rate} is inconsistent"):
            lyric_summary(broken, **META)


def test_non_dict_report_is_rejected():
    with pytest.raises(ValueError, match="lyric transcription report"):
        lyric_summary([], **META)


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["lyric_evaluator", *map(str, args)])
    main()


def cli_args(tmp_path):
    pairs = tmp_path / "pairs.json"
    pairs.write_text(
        json.dumps([{"reference": "青い空", "hypothesis": "青い空"}]), encoding="utf-8"
    )
    return pairs, tmp_path / "detail.json", tmp_path / "summary.json"


def test_cli_writes_detail_and_summary_separately(tmp_path, monkeypatch):
    pairs, detail, summary = cli_args(tmp_path)
    run_cli(
        monkeypatch,
        pairs, "--output", detail, "--summary-output", summary,
        "--dataset-id", "d", "--dataset-version", "1", "--model-name", "m",
        "--model-version", "1", "--evaluation-kind", "synthetic",
    )  # fmt: skip
    written = json.loads(summary.read_text(encoding="utf-8"))
    assert (
        written["dataset"]["sha256"] == __import__("hashlib").sha256(pairs.read_bytes()).hexdigest()
    )
    assert "pairs" not in written and json.loads(detail.read_text(encoding="utf-8"))["cer"] == 0


def test_cli_writes_neither_file_when_the_summary_destination_is_unusable(
    tmp_path, monkeypatch, capsys
):
    pairs, detail, _ = cli_args(tmp_path)
    detail.write_text("previous report", encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        run_cli(
            monkeypatch,
            pairs,
            "--output", detail, "--summary-output", blocker / "sum.json",
            "--dataset-id", "d", "--dataset-version", "1", "--model-name", "m",
            "--model-version", "1", "--evaluation-kind", "synthetic",
        )  # fmt: skip
    assert exit_info.value.code == 2
    # 要約の出力先が使えないことが原因で失敗し、既存の詳細レポートは変更されない。
    assert "blocker" in capsys.readouterr().err
    assert detail.read_text(encoding="utf-8") == "previous report"
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_rejects_metadata_without_summary_output(tmp_path, monkeypatch, capsys):
    pairs, detail, _ = cli_args(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, pairs, "--output", detail, "--dataset-id", "d")
    assert exit_info.value.code == 2
    assert "requires --summary-output" in capsys.readouterr().err
    assert not detail.exists()


def test_cli_rejects_summary_path_overwriting_input_or_detail(tmp_path, monkeypatch, capsys):
    pairs, detail, _ = cli_args(tmp_path)
    original = pairs.read_bytes()
    for target in (pairs, detail):
        with pytest.raises(SystemExit) as exit_info:
            run_cli(
                monkeypatch,
                pairs, "--output", detail, "--summary-output", target,
                "--dataset-id", "d", "--dataset-version", "1", "--model-name", "m",
                "--model-version", "1", "--evaluation-kind", "synthetic",
            )  # fmt: skip
        assert exit_info.value.code == 2
        assert "Summary path must differ" in capsys.readouterr().err
    assert pairs.read_bytes() == original
