import json
from threading import Event

import pytest

from open_karaoke_analysis.adapters import AnalysisContext
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.note_evaluator import _case_documents
from open_karaoke_analysis.note_optimizer import estimate_key
from open_karaoke_analysis.notes import NotesAdapter


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _two_note_case():
    return {
        "id": "adapter-two-note",
        "duration_samples": 48_000,
        "pitch_regions": [
            {
                "start_sample": 0,
                "end_sample": 24_000,
                "midi": 60,
                "contour_cents": [0, 6, -6],
            },
            {
                "start_sample": 24_000,
                "end_sample": 48_000,
                "midi": 62,
                "contour_cents": [0, 7, -7],
            },
        ],
        "note_boundaries": [{"sample": 24_000, "probability": 0.72}],
        "reference_notes": [],
    }


def _context(tmp_path, progress=None):
    reports = progress if progress is not None else []
    return AnalysisContext(
        root=tmp_path,
        output_prefix="jobs/notes/result",
        input_path=None,
        cancelled=Event(),
        report=lambda value, stage: reports.append((value, stage)),
    )


def test_notes_adapter_publishes_canonical_notes_candidates_and_evidence(tmp_path):
    pitch, events = _case_documents(_two_note_case())
    _write(tmp_path / "pitch.json", pitch)
    _write(tmp_path / "events.json", events)
    _write(
        tmp_path / "alignment.json",
        {
            "format_version": 1,
            "duration_samples": 48_000,
            "input_sha256": pitch["input_sha256"],
            "lines": [
                {"id": "lyric-1", "start_sample": 0, "end_sample": 24_000},
                {"id": "lyric-2", "start_sample": 24_000, "end_sample": 48_000},
            ],
        },
    )
    _write(
        tmp_path / "candidates.json",
        {
            "format_version": 1,
            "sample_rate": 48_000,
            "duration_samples": 48_000,
            "input_sha256": pitch["input_sha256"],
            "provider": {"id": "basic-pitch", "version": "external-fixture"},
            "candidates": [
                {
                    "id": "bp-1",
                    "start_sample": 0,
                    "end_sample": 24_000,
                    "midi_note": 60,
                    "confidence": 0.8,
                },
                {
                    "id": "bp-2",
                    "start_sample": 24_000,
                    "end_sample": 48_000,
                    "midi_note": 62,
                    "confidence": 0.8,
                },
            ],
        },
    )
    _write(
        tmp_path / "repeats.json",
        {
            "format_version": 1,
            "duration_samples": 48_000,
            "pairs": [
                {
                    "id": "repeat-1",
                    "source_start_sample": 0,
                    "source_end_sample": 24_000,
                    "repeat_start_sample": 24_000,
                    "repeat_end_sample": 48_000,
                    "confidence": 0.8,
                }
            ],
        },
    )

    reports = []
    artifacts = NotesAdapter().analyze(
        _context(tmp_path, reports),
        {
            "pitch_artifact": "pitch.json",
            "vocal_event_artifact": "events.json",
            "alignment_artifact": "alignment.json",
            "candidate_artifact": "candidates.json",
            "repeated_sections_artifact": "repeats.json",
        },
    )
    assert [path.rsplit("/", 1)[-1] for path in artifacts] == [
        "notes.json",
        "note_candidates.json",
        "note_evidence.json",
    ]

    output_root = tmp_path / "jobs" / "notes" / "result"
    notes = json.loads((output_root / "notes.json").read_text(encoding="utf-8"))
    candidates = json.loads((output_root / "note_candidates.json").read_text(encoding="utf-8"))
    evidence = json.loads((output_root / "note_evidence.json").read_text(encoding="utf-8"))

    assert [note["midi_note"] for note in notes["notes"]] == [60, 62]
    assert [note["lyric_segment_id"] for note in notes["notes"]] == ["lyric-1", "lyric-2"]
    assert "basic-pitch" in candidates["candidate_sources"]
    assert candidates["provider_contract_version"] == 1
    assert evidence["optimizer"]["id"] == "temporal-dp-v1"
    assert evidence["optimizer"]["replaceable"] is True
    assert evidence["optimizer"]["repetition_prior_count"] == 1
    assert evidence["candidate_provider_contract"]["external_weights_bundled"] is False
    assert evidence["real_singing_accuracy_verified"] is False
    assert reports[-1][1] == "publishing_score"


def test_notes_adapter_rejects_mismatched_pitch_and_event_snapshots(tmp_path):
    pitch, events = _case_documents(_two_note_case())
    events["input_sha256"] = "different-snapshot"
    _write(tmp_path / "pitch.json", pitch)
    _write(tmp_path / "events.json", events)

    with pytest.raises(ServiceError) as captured:
        NotesAdapter().analyze(
            _context(tmp_path),
            {
                "pitch_artifact": "pitch.json",
                "vocal_event_artifact": "events.json",
            },
        )
    assert captured.value.detail.code == "invalid_score_evidence"


def test_key_estimation_uses_soft_weighted_pitch_class_profile():
    candidates = [
        {
            "base_midi": midi,
            "start_sample": index * 4800,
            "end_sample": (index + 1) * 4800,
            "pitch_confidence": 0.9,
        }
        for index, midi in enumerate([60, 62, 64, 65, 67, 69, 71, 72])
    ]
    result = estimate_key(candidates)
    assert result["tonic"] == "C"
    assert result["mode"] == "major"
    assert 0.0 <= result["confidence"] <= 1.0


def test_notes_options_are_strict_and_require_upstream_artifacts():
    adapter = NotesAdapter()
    with pytest.raises(ServiceError) as missing:
        adapter.validate_options({})
    assert missing.value.detail.code == "invalid_options"

    with pytest.raises(ServiceError) as unknown:
        adapter.validate_options(
            {
                "pitch_artifact": "pitch.json",
                "vocal_event_artifact": "events.json",
                "unknown": True,
            }
        )
    assert unknown.value.detail.code == "invalid_options"
