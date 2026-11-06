"""Contract tests for the versioned quality report (#108.1). Synthetic signals only."""

import json

import pytest

from open_karaoke_analysis.quality_report import (
    AlignmentCoverageSignal,
    Attempt,
    BoundarySignal,
    CalibrationState,
    InputRef,
    IntegrityCheck,
    LyricSegmentSignal,
    ModelRef,
    Playability,
    QualityFlag,
    QualityOptions,
    QualityReportError,
    assess_signals,
    build_quality_report,
    report_from_json,
    report_to_json,
)

H = "a" * 64
OPTS = QualityOptions()


def _report(flags=(), attempt=None):
    """
    {
      責務: [_report: テスト用レポートを組む]
      処理: [1: 固定入力で組立]
      引数: [flags: 指摘, attempt: 出典]
      戻り値: [QualityReport]
    }
    """
    return build_quality_report(
        options=OPTS,
        assessor_version="1",
        inputs=(InputRef("lyrics", 2, "b" * 64), InputRef("alignment", 1, H)),
        stage_models={"lyrics": ModelRef("whisper_x", "1", None, H)},
        flags=flags,
        attempt=attempt,
    )


def _signals():
    """
    {
      責務: [_signals: 全診断が出る合成信号からflagを作る]
      処理: [1: assess_signalsを呼ぶ]
      引数: []
      戻り値: [flagのタプル]
    }
    """
    return assess_signals(
        OPTS,
        lyrics=(LyricSegmentSignal(48000, 96000, 0.9, 0.95), LyricSegmentSignal(0, 10, 0.1, 0.1)),
        coverage=(AlignmentCoverageSignal(0, 48000, 0.2), AlignmentCoverageSignal(0, 5, 0.9)),
        boundaries=(BoundarySignal("notes", 100, 130, 30), BoundarySignal("notes", 200, 210, 0)),
    )


def test_synthetic_signals_become_diagnostic_flags_with_warnings_only():
    flags = _signals()
    assert sorted(f.code for f in flags) == [
        "alignment_low_coverage",
        "boundary_inconsistent",
        "whisper_hallucination",
        "whisper_repeat",
    ]
    assert all(f.calibration_state is CalibrationState.DIAGNOSTIC for f in flags)
    assert _report(flags).playability is Playability.PLAYABLE_WITH_WARNINGS


def test_clean_input_is_playable_and_failed_integrity_blocks():
    assert _report().playability is Playability.PLAYABLE
    flags = assess_signals(OPTS, integrity=(IntegrityCheck("notes", "monotonic", False, 0, 10),))
    assert flags[0].calibration_state is CalibrationState.HARD_INTEGRITY
    assert _report(flags).playability is Playability.BLOCKED
    passed = assess_signals(OPTS, integrity=(IntegrityCheck("notes", "monotonic", True, 0, 10),))
    assert passed == ()


def test_serialization_is_deterministic_and_roundtrips():
    text = report_to_json(_report(_signals()))
    assert text == report_to_json(_report(tuple(reversed(_signals()))))
    assert report_to_json(report_from_json(text)) == text
    assert json.loads(text)["format_version"] == 1
    assert "probability" not in text and "confidence" not in text


@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "g" * 64])
def test_invalid_hash_fails_closed(bad):
    with pytest.raises(QualityReportError):
        InputRef("lyrics", 1, bad)
    data = json.loads(report_to_json(_report()))
    data["inputs"][0]["sha256"] = bad
    with pytest.raises(QualityReportError):
        report_from_json(json.dumps(data))


def _mutated(mutate):
    """
    {
      責務: [_mutated: 正しいJSONを改変して読み込む]
      処理: [1: 改変, 2: 読込]
      引数: [mutate: 改変関数]
      戻り値: [QualityReport]
    }
    """
    data = json.loads(report_to_json(_report(_signals())))
    mutate(data)
    return report_from_json(json.dumps(data))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format_version=2),
        lambda d: d.update(format_version=True),
        lambda d: d.update(extra=1),
        lambda d: d.update(report_sha256="c" * 64),
        lambda d: d.update(playability="blocked"),
        lambda d: d.update(warning_codes=[]),
        lambda d: d["flags"][0].update(probability=0.5),
        lambda d: d["flags"][0].update(calibration_state="calibrated"),
        lambda d: d["flags"][0].update(start_sample=1.5),
        lambda d: d["options"].update(repeat_ratio_min=0.1),
        lambda d: d["flags"].reverse(),
        lambda d: d["flags"][0].update(heuristic_score=1.5),
    ],
)
def test_tampered_or_invalid_reports_are_rejected(mutate):
    with pytest.raises(QualityReportError):
        _mutated(mutate)


def test_non_finite_json_is_rejected():
    text = report_to_json(_report()).replace('"pass_index":0', '"pass_index":NaN')
    with pytest.raises(QualityReportError):
        report_from_json(text)
    with pytest.raises(QualityReportError):
        report_from_json("{not json")


def _flag(state, **kwargs):
    """
    {
      責務: [_flag: 校正状態規則のテスト用flagを作る]
      処理: [1: 既定値に上書きして生成]
      引数: [state: 校正状態, kwargs: 上書き]
      戻り値: [QualityFlag]
    }
    """
    base = {
        "code": "whisper_repeat",
        "stage": "lyrics",
        "start_sample": 0,
        "end_sample": 10,
        "diagnostic": "x",
        "heuristic_score": 0.5,
    }
    return QualityFlag(calibration_state=state, **{**base, **kwargs})


def test_calibration_state_rules():
    with pytest.raises(QualityReportError):
        _flag(CalibrationState.CALIBRATED)
    _flag(CalibrationState.CALIBRATED, calibration_id="c1")
    with pytest.raises(QualityReportError):
        _flag(CalibrationState.DIAGNOSTIC, calibration_id="c1")
    with pytest.raises(QualityReportError):
        _flag(CalibrationState.HARD_INTEGRITY, code="integrity_violation")
    with pytest.raises(QualityReportError):
        _flag(CalibrationState.HARD_INTEGRITY, heuristic_score=None)
    _flag(CalibrationState.HARD_INTEGRITY, code="integrity_violation", heuristic_score=None)


@pytest.mark.parametrize("start,end", [(5, 5), (-1, 3), (10, 2), (0.5, 3)])
def test_sample_range_must_be_ordered_nonnegative_integers(start, end):
    with pytest.raises(QualityReportError):
        _flag(CalibrationState.DIAGNOSTIC, start_sample=start, end_sample=end)


def test_attempt_provenance_rules():
    assert _report(attempt=Attempt(1, "repair", H)).attempt.kind == "repair"
    for args in [(0, "repair", H), (1, "repair", None), (1, "initial", None), (0, "initial", H)]:
        with pytest.raises(QualityReportError):
            Attempt(*args)


def test_options_hash_is_recorded_and_threshold_change_alters_it():
    assert _report().assessor.options_sha256 == OPTS.sha256()
    assert QualityOptions(repeat_ratio_min=0.6).sha256() != OPTS.sha256()
    with pytest.raises(QualityReportError):
        QualityOptions(repeat_ratio_min=2)


def test_invalid_signals_and_builder_inputs_are_rejected():
    with pytest.raises(QualityReportError):
        assess_signals(OPTS, lyrics=(LyricSegmentSignal(0, 10, float("nan"), 0.1),))
    with pytest.raises(QualityReportError):
        assess_signals(OPTS, boundaries=(BoundarySignal("notes", 0, 10, -1),))
    with pytest.raises(QualityReportError):
        build_quality_report(
            options=OPTS,
            assessor_version="1",
            inputs=(),
            flags=(),
            stage_models={"bogus": ModelRef("m", "1", None, H)},
        )
    with pytest.raises(QualityReportError):
        build_quality_report(
            options=OPTS,
            assessor_version="1",
            flags=(),
            inputs=(InputRef("a", 1, H), InputRef("a", 1, H)),
            stage_models={},
        )


def test_duplicate_keys_and_wrong_container_types_are_rejected():
    text = report_to_json(_report())
    with pytest.raises(QualityReportError):
        report_from_json(text.replace('"attempt":', '"attempt":{},"attempt":', 1))
    for bad in ("[]", "1", "null", '"x"'):
        with pytest.raises(QualityReportError):
            report_from_json(bad)
    for key, value in (("inputs", [1]), ("flags", ["x"]), ("stage_models", [[]]), ("assessor", 5)):
        data = json.loads(text)
        data[key] = value
        with pytest.raises(QualityReportError):
            report_from_json(json.dumps(data))
    data = json.loads(report_to_json(_report(_signals())))
    data["flags"][0]["stage"] = ["lyrics"]
    with pytest.raises(QualityReportError):
        report_from_json(json.dumps(data))
    data = json.loads(report_to_json(_report(_signals())))
    data["flags"][0]["heuristic_score"] = True
    with pytest.raises(QualityReportError):
        report_from_json(json.dumps(data))
