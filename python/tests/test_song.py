"""Orchestration tests: fixture model stages never imply actual singing accuracy."""

import hashlib
import json
import shutil
import time
from threading import Event
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AdapterRegistry, AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ModelInfo, Role, ServiceError
from open_karaoke_analysis.song import STAGES, SongAdapter


class FixtureStage:
    def __init__(self, stage, calls, *, fault=None):
        self.info = ModelInfo(id=f"fixture-{stage}", version="test-only", role=Role(stage))
        self.calls = calls
        self.fault = fault

    def validate_options(self, options):
        return options

    def analyze(self, context, options):
        stage = self.info.role.value
        self.calls.append(stage)
        if self.fault == "wait":
            while not context.cancelled.wait(0.005):
                context.report(0.2, "fixture_wait")
            context.checkpoint()
        if self.fault == "failure":
            context.write_json("partial.json", {"fixture": True})
            raise ServiceError("fixture_failure", "Injected stage failure")
        if self.fault == "cancel":
            context.cancelled.set()
            context.checkpoint()
        root = context.root / context.output_prefix
        root.mkdir(parents=True, exist_ok=True)
        duration = sf.info(context.input_path).frames
        digest = hashlib.sha256(context.input_path.read_bytes()).hexdigest()
        version = {"format_version": 1, "analysis_version": options.get("analysis_version", 1)}
        metadata = {
            **version,
            "sample_rate": 48_000,
            "duration_samples": duration,
            "input_sha256": "0" * 64 if self.fault == "hash" else digest,
        }
        if self.fault == "duration":
            metadata["duration_samples"] += 1
        segment = {
            "id": "line-1",
            "text": "あ",
            "start_sample": 0,
            "end_sample": duration,
            "text_confidence": None,
            "timing_confidence": None,
            "source": "fixture",
        }
        lyrics = {**version, "segments": [segment]}
        if stage == "stems":
            artifacts = []
            for name in ("vocals.wav", "accompaniment.wav"):
                shutil.copyfile(context.input_path, root / name)
                artifacts.append(f"{context.output_prefix}/{name}")
            return [*artifacts, context.write_json("stems.json", metadata)]
        if stage == "lyrics":
            return [
                context.write_json("lyrics.json", lyrics),
                context.write_json("asr_raw.json", metadata),
            ]
        if stage == "alignment":
            if self.fault == "span":
                segment["end_sample"] += 1
            if self.fault == "duplicate":
                lyrics["segments"].append(segment.copy())
            return [
                context.write_json("lyrics.json", lyrics),
                context.write_json("alignment.json", {**metadata, "lines": [segment]}),
                context.write_json("alignment_evidence.json", {**version, "items": []}),
            ]
        if stage == "pitch":
            return [context.write_json("pitch.json", metadata)]
        if stage == "events":
            assert (context.root / options["pitch_artifact"]).is_file()
            assert (context.root / options["alignment_artifact"]).is_file()
            return [context.write_json("vocal_events.json", metadata)]
        assert stage == "notes"
        for key in ("pitch_artifact", "vocal_event_artifact", "alignment_artifact"):
            assert (context.root / options[key]).is_file()
        note = {
            "id": "note-1",
            "start_sample": 0,
            "end_sample": duration,
            "midi_note": 69,
            "pitch_confidence": None,
            "lyric_segment_id": "missing" if self.fault == "reference" else "line-1",
        }
        return [
            context.write_json("notes.json", {**version, "notes": [note]}),
            context.write_json("note_evidence.json", metadata),
        ]


def fixture_registry(calls, stage=None, fault=None):
    return AdapterRegistry(
        [FixtureStage(name, calls, fault=fault if name == stage else None) for name in STAGES]
    )


@pytest.fixture
def source(tmp_path):
    mono = 0.2 * np.sin(2 * np.pi * 440 * np.arange(9600) / 48_000)
    path = tmp_path / "input.wav"
    sf.write(path, np.repeat(mono[:, None], 2, axis=1), 48_000, subtype="FLOAT")
    return path


def context(tmp_path, source, report=None):
    return AnalysisContext(
        tmp_path, f"jobs/{uuid4()}/{uuid4()}", source, Event(), report or (lambda p, s: None)
    )


def result(tmp_path, artifacts):
    return json.loads((tmp_path / artifacts[-1]).read_text(encoding="utf-8"))


def test_pipeline_order_provenance_reference_and_immutable_input(tmp_path, source):
    calls, progress = [], []
    (tmp_path / "reference.txt").write_text("あ\n参考歌詞", encoding="utf-8")
    ctx = context(tmp_path, source, lambda p, s: progress.append((p, s)))
    adapter = SongAdapter(fixture_registry(calls))
    artifacts = adapter.analyze(
        ctx,
        {"title": "テスト", "analysis_version": 3, "reference_lyrics_artifact": "reference.txt"},
    )
    doc = result(tmp_path, artifacts)
    assert calls == list(STAGES)
    assert all(p < 1 for p, _ in progress)
    assert [p for p, _ in progress] == sorted(p for p, _ in progress)
    assert doc["bundle"]["song"]["duration_samples"] == 9600
    assert doc["bundle"]["lyrics"]["analysis_version"] == 3
    assert doc["bundle"]["notes"]["notes"][0]["lyric_segment_id"] == "line-1"
    provenance = doc["bundle"]["evidence"]["items"][0]["evidence"]
    assert provenance["singing_accuracy_verified"] is False
    assert [s["model"]["role"] for s in provenance["stages"]] == list(STAGES)
    for asset in doc["assets"]:
        data = (tmp_path / asset["source"]).read_bytes()
        assert len(data) == asset["bytes"]
        assert hashlib.sha256(data).hexdigest() == asset["sha256"]
    source.write_bytes(b"replaced after analysis")
    snapshot = next(a for a in doc["assets"] if a["destination"] == "audio/original_48k.wav")
    copied = (tmp_path / snapshot["source"]).read_bytes()
    assert hashlib.sha256(copied).hexdigest() == snapshot["sha256"]
    assert any(a["destination"].endswith("reference.txt") for a in doc["assets"])
    assert not (tmp_path / "songs").exists()


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("fault", ["failure", "cancel"])
def test_failure_and_cancel_never_publish_result(tmp_path, source, stage, fault):
    calls = []
    adapter = SongAdapter(fixture_registry(calls, stage, fault))
    with pytest.raises((ServiceError, AnalysisCancelled)):
        adapter.analyze(context(tmp_path, source), {"title": "test"})
    assert calls == list(STAGES[: STAGES.index(stage) + 1])
    assert not list(tmp_path.rglob("song-result.json"))
    assert not (tmp_path / "songs").exists()


@pytest.mark.parametrize(
    "stage,fault",
    [
        ("stems", "hash"),
        ("lyrics", "hash"),
        ("alignment", "hash"),
        ("pitch", "hash"),
        ("events", "duration"),
        ("notes", "hash"),
        ("alignment", "span"),
        ("alignment", "duplicate"),
        ("notes", "reference"),
    ],
)
def test_inconsistent_outputs_rejected(tmp_path, source, stage, fault):
    adapter = SongAdapter(fixture_registry([], stage, fault))
    with pytest.raises(ServiceError):
        adapter.analyze(context(tmp_path, source), {"title": "test"})
    assert not list(tmp_path.rglob("song-result.json"))


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"title": " "},
        {"title": "x", "analysis_version": True},
        {"title": "x", "models": {"song": "nested"}},
        {"title": "x", "stage_options": {"notes": {"pitch_artifact": "other.json"}}},
        {"title": "x", "stage_options": {"lyrics": {"analysis_version": 3}}},
    ],
)
def test_bad_options(options):
    with pytest.raises(ServiceError):
        SongAdapter(fixture_registry([])).validate_options(options)


def test_model_selection_fails_before_inference(tmp_path, source):
    calls = []
    adapter = SongAdapter(fixture_registry(calls))
    with pytest.raises(ServiceError) as error:
        adapter.analyze(
            context(tmp_path, source), {"title": "x", "models": {"notes": "not-installed"}}
        )
    assert error.value.detail.code == "model_not_found" and not calls
    with pytest.raises(ServiceError) as error:
        adapter.analyze(
            context(tmp_path, source), {"title": "x", "models": {"notes": "fixture-pitch"}}
        )
    assert error.value.detail.code == "model_role_mismatch" and not calls


def test_pipeline_with_real_pitch_events_and_notes(tmp_path, source):
    from open_karaoke_analysis.notes import NotesAdapter
    from open_karaoke_analysis.pitch import PitchAdapter
    from open_karaoke_analysis.vocal_events import VocalEventAdapter

    registry = AdapterRegistry(
        [FixtureStage(s, []) for s in STAGES[:3]]
        + [PitchAdapter(), VocalEventAdapter(), NotesAdapter()]
    )
    artifacts = SongAdapter(registry).analyze(
        context(tmp_path, source), {"title": "synthetic-tone"}
    )
    doc = result(tmp_path, artifacts)
    assert doc["bundle"]["notes"]["notes"]
    assert all(abs(n["midi_note"] - 69) <= 1 for n in doc["bundle"]["notes"]["notes"])


def test_service_cancel_cleans_every_stage_and_recovers(tmp_path, source):
    from fastapi.testclient import TestClient

    from open_karaoke_analysis.service import create_app

    calls = []
    registry = fixture_registry(calls, "alignment", "wait")
    adapter = SongAdapter(registry)
    token = "x" * 64
    app = create_app(tmp_path, token, [adapter])
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        request = {"input_artifact": source.name, "options": {"title": "test"}}
        response = client.post("/analysis/song", json=request)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        deadline = time.monotonic() + 10
        while "alignment" not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert client.post(f"/jobs/{job_id}/cancel").status_code == 200
        while True:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["status"] == "cancelled" and job["artifacts"] == []
        assert not (tmp_path / f"jobs/{job['instance_id']}/{job_id}").exists()
        assert source.is_file()
        registry.adapters["fixture-alignment"].fault = None
        new_id = client.post("/analysis/song", json=request).json()["id"]
        while True:
            job = client.get(f"/jobs/{new_id}").json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["status"] == "completed", job


def test_fingerprint_stable_across_jobs_but_changes_with_version(tmp_path, source):
    adapter = SongAdapter(fixture_registry([]))

    def fingerprint(version):
        artifacts = adapter.analyze(
            context(tmp_path, source), {"title": "test", "analysis_version": version}
        )
        evidence = result(tmp_path, artifacts)["bundle"]["evidence"]["items"][0]["evidence"]
        return evidence["analysis_fingerprint"]

    assert fingerprint(1) == fingerprint(1)
    assert fingerprint(1) != fingerprint(2)


def test_later_adapter_cannot_mutate_an_earlier_result(tmp_path, source):
    registry = fixture_registry([])
    delegate = registry.adapters["fixture-notes"].analyze

    def mutation(ctx, options):
        artifacts = delegate(ctx, options)
        path = ctx.root / options["pitch_artifact"]
        path.write_bytes(path.read_bytes() + b" ")
        return artifacts

    registry.adapters["fixture-notes"].analyze = mutation
    with pytest.raises(ServiceError) as error:
        SongAdapter(registry).analyze(context(tmp_path, source), {"title": "test"})
    assert error.value.detail.code == "song_input_changed"


def test_unowned_or_missing_outputs_are_rejected(tmp_path, source):
    for produced in [[source.name], [], ["../outside.json"]]:
        registry = fixture_registry([])
        registry.adapters["fixture-notes"].analyze = lambda ctx, options, p=produced: p
        with pytest.raises(ServiceError):
            SongAdapter(registry).analyze(context(tmp_path, source), {"title": "test"})
    assert not list(tmp_path.rglob("song-result.json"))


@pytest.mark.parametrize(
    "rate,channels,subtype",
    [(44_100, 2, "FLOAT"), (48_000, 1, "FLOAT"), (48_000, 2, "PCM_16")],
)
def test_noncanonical_audio_rejected(tmp_path, rate, channels, subtype):
    path = tmp_path / "noncanonical.wav"
    sf.write(path, np.zeros((100, channels)), rate, subtype=subtype)
    with pytest.raises(ServiceError) as error:
        SongAdapter(fixture_registry([])).analyze(context(tmp_path, path), {"title": "test"})
    assert error.value.detail.code == "invalid_audio"
