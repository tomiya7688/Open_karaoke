import json
from itertools import groupby
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.alignment import AlignmentAdapter, aggregate_units, align_segment
from open_karaoke_analysis.alignment_text import normalized_chars, read_lyrics
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.ctc_alignment import align_tokens, frame_samples


def emission(path, classes=4):
    values = np.full((len(path), classes), 0.01 / (classes - 1))
    values[np.arange(len(path)), path] = 0.99
    return np.log(values)


class FixtureBackend:
    vocabulary = {"あ": 1, "い": 2, "a": 1, "b": 2}
    blank_id = 0

    def prepare(self, context, options):
        context.checkpoint()

    def emissions(self, samples):
        return emission([0, 1, 1, 0, 2, 2, 0])

    def identity(self):
        return {"id": "scripted-ctc-fixture", "revision": "1", "synthetic": True}

    def close(self):
        pass


@pytest.fixture
def case(tmp_path):
    audio = 0.1 * np.sin(2 * np.pi * 220 * np.arange(48000) / 48000)
    sf.write(tmp_path / "vocals.wav", audio, 48000, subtype="FLOAT")
    lyrics = {
        "format_version": 1,
        "analysis_version": 1,
        "segments": [
            {
                "id": "line-1",
                "text": "あい",
                "start_sample": 0,
                "end_sample": 48000,
                "text_confidence": None,
                "timing_confidence": None,
                "source": "asr-fixture",
            }
        ],
    }
    (tmp_path / "input.json").write_text(json.dumps(lyrics), encoding="utf-8")
    context = AnalysisContext(
        tmp_path, "jobs/alignment/fixture", tmp_path / "vocals.wav", Event(), lambda *_: None
    )
    adapter = AlignmentAdapter(FixtureBackend)
    return tmp_path, context, adapter, lyrics


def run(case, extra=None):
    root, context, adapter, _ = case
    options = adapter.validate_options({"lyrics_artifact": "input.json", **(extra or {})})
    paths = adapter.analyze(context, options)
    return [json.loads((root / path).read_text(encoding="utf-8")) for path in paths]


def test_viterbi_duration_repeated_labels_and_gaps():
    path = [0, 1, 1, 0, 1, 1, 0, 2, 2, 0]
    result = align_tokens(emission(path), [1, 1, 2], 0, lambda: None)
    assert [(x.start_frame, x.end_frame) for x in result] == [(1, 3), (4, 6), (7, 9)]
    assert all(x.score == pytest.approx(0.99) for x in result)
    assert align_tokens(emission([1, 1]), [1, 1], 0, lambda: None) is None


def test_viterbi_matches_exhaustive_small_ctc_search():
    from itertools import product

    rng = np.random.default_rng(43)
    probabilities = rng.dirichlet(np.ones(3), size=5)
    lp = np.log(probabilities)
    tokens = [1, 1]
    scores = []
    for path in product(range(3), repeat=5):
        collapsed = [k for k, _ in groupby(path) if k != 0]
        if collapsed == tokens:
            scores.append((sum(lp[t, k] for t, k in enumerate(path)), path))
    _, best = max(scores)
    result = align_tokens(lp, tokens, 0, lambda: None)
    positions = []
    offset = 0
    for k, group in groupby(best):
        count = len(list(group))
        if k:
            positions.append((offset, offset + count))
        offset += count
    assert [(s.start_frame, s.end_frame) for s in result] == positions


@pytest.mark.parametrize("tokens,blank", [([0], 0), ([True], 0), ([4], 0), ([1], -1)])
def test_bad_token_ids(tokens, blank):
    with pytest.raises(ServiceError):
        align_tokens(emission([1]), tokens, blank, lambda: None)


@pytest.mark.parametrize(
    "values",
    [np.array([[np.nan, -1.0]]), np.zeros((2, 2)), np.array([[np.inf, -1]]), np.zeros((0, 2))],
)
def test_bad_emissions(values):
    with pytest.raises(ServiceError):
        align_tokens(values, [1], 0, lambda: None)


def test_cancellation_in_trellis():
    def stop():
        raise AnalysisCancelled

    with pytest.raises(AnalysisCancelled):
        align_tokens(emission([1] * 100), [1], 0, stop)


def test_exact_integer_sample_conversion():
    assert frame_samples(1, 2, 3, 100, 110) == (103, 107)
    with pytest.raises(ValueError):
        frame_samples(2, 1, 3, 100, 110)


def test_pipeline_without_reference(case):
    lyrics, report, evidence = run(case)
    assert report["inference_performed"] is True
    assert report["aligned_units"] == report["total_units"] == 2
    assert lyrics["segments"][0]["text"] == "あい"
    assert 0 < lyrics["segments"][0]["start_sample"] < lyrics["segments"][0]["end_sample"] < 48000
    assert lyrics["segments"][0]["timing_confidence"] is None
    assert report["asr_original"] == case[3]
    assert evidence["items"][1]["evidence"]["timing_kind"] == "ctc_forced"
    assert report["reference"] is None
    assert not list(case[0].rglob(".alignment-*"))


def test_imported_line_breaks_and_unmatched_reference(case):
    (case[0] / "reference.txt").write_bytes("あ\nい\nう\n".encode())
    _, report, _ = run(case, {"reference_lyrics_artifact": "reference.txt"})
    first, second, missing = report["reference_lines"]
    assert first["status"] == second["status"] == "matched"
    assert missing["status"] == "unaligned" and missing["start_sample"] is None
    assert report["reference"]["text"] == "あ\nい\nう\n"
    assert any(op["operation"] == "insert" for op in report["reference_mapping"]["operations"])
    assert report["reference"]["authoritative"] is False


def test_silence_never_loads_model_and_preserves_coarse_asr(case):
    class NoModel(FixtureBackend):
        def prepare(self, *_):
            pytest.fail("Silence must not load a model")

    root, context, _, original = case
    sf.write(root / "vocals.wav", np.zeros(48000), 48000, subtype="FLOAT")
    lyrics, report, evidence = run((root, context, AlignmentAdapter(NoModel), original))
    assert report["inference_performed"] is False
    assert report["aligned_units"] == 0
    assert lyrics["segments"][0]["start_sample"] == 0
    assert evidence["items"][1]["evidence"]["timing_kind"] == "coarse_asr_fallback"
    assert all(u["start_sample"] is None for u in report["lines"][0]["words"])


def test_low_score_and_oov_not_invented(case):
    _, context, adapter, original = case
    options = adapter.validate_options({"lyrics_artifact": "input.json", "min_token_score": 1.0})
    line = align_segment(
        original["segments"][0], emission([0, 1, 2]), {"あ": 1, "い": 2}, 0, options, context
    )
    assert line["status"] == "unaligned"
    assert line["characters"][0]["observed_span"] is not None
    line = align_segment(
        original["segments"][0], emission([0, 1, 2]), {"あ": 1}, 0, options, context
    )
    assert "out_of_vocabulary" in line["flags"]
    assert all(c["start_sample"] is None for c in line["characters"])


@pytest.mark.parametrize(
    "change",
    [
        {"start_sample": True},
        {"end_sample": 0},
        {"end_sample": 999999},
        {"id": " "},
        {"text_confidence": float("nan")},
        {"start_sample": -1},
    ],
)
def test_invalid_lyrics(case, change):
    root, _, _, lyrics = case
    lyrics["segments"][0].update(change)
    (root / "input.json").write_text(json.dumps(lyrics), encoding="utf-8")
    with pytest.raises(ServiceError):
        run(case)


def test_unordered_duplicate_ids_and_future_version(case):
    root, context, _, lyrics = case
    lyrics["segments"] *= 2
    (root / "input.json").write_text(json.dumps(lyrics), encoding="utf-8")
    with pytest.raises(ServiceError):
        read_lyrics(context, "input.json")
    lyrics["format_version"] = 2
    (root / "input.json").write_text(json.dumps(lyrics), encoding="utf-8")
    with pytest.raises(ServiceError):
        run(case)


def test_nan_in_audio_gap_is_rejected(case):
    root, _, _, _ = case
    samples = np.zeros(96000, dtype=np.float32)
    samples[-1] = np.nan
    sf.write(root / "vocals.wav", samples, 48000, subtype="FLOAT")
    with pytest.raises(ServiceError, match="NaN"):
        run(case)


def test_cancellation_before_publication(case):
    case[1].cancelled.set()
    with pytest.raises(AnalysisCancelled):
        run(case)
    assert not (case[0] / "jobs").exists()


def test_text_normalization_offsets():
    chars = normalized_chars("ｶﾞ e\u0301、Ａ")
    assert "".join(c["char"] for c in chars) == "ガéa"
    assert (chars[0]["char_start"], chars[0]["char_end"]) == (0, 2)


def test_whitespace_word_aggregation():
    chars = [
        {
            **c,
            "status": "aligned",
            "score": 0.9,
            "start_sample": i * 100,
            "end_sample": i * 100 + 50,
        }
        for i, c in enumerate(normalized_chars("ab a"))
    ]
    words = aggregate_units("ab a", chars, "whitespace_word", "s")
    assert [w["text"] for w in words] == ["ab", "a"]
    assert words[0]["end_sample"] == 150


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"lyrics_artifact": "x", "language": "en"},
        {"lyrics_artifact": "x", "min_token_score": float("nan")},
        {"lyrics_artifact": "x", "unknown": True},
    ],
)
def test_invalid_options(options):
    with pytest.raises(ServiceError):
        AlignmentAdapter().validate_options(options)


def test_python_output_matches_rust_fixture(case):
    from pathlib import Path

    lyrics, _, _ = run(case)
    path = Path(__file__).parents[2] / "core/tests/fixtures/aligned_lyrics.json"
    assert lyrics == json.loads(path.read_text(encoding="utf-8"))


def test_nonfinite_extension_json_is_rejected(case):
    root, context, _, lyrics = case
    text = json.dumps(lyrics)[:-1] + ', "future_extension": 1e999}'
    (root / "input.json").write_text(text, encoding="utf-8")
    with pytest.raises(ServiceError):
        read_lyrics(context, "input.json")
