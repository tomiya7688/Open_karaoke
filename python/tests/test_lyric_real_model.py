"""Opt-in real checkpoint smoke. Generated tone is NOT a singing accuracy dataset."""

import json
import os
from pathlib import Path

import pytest
from test_lyrics import setup_audio

from open_karaoke_analysis.lyrics import LyricsAdapter
from open_karaoke_analysis.whisper_backend import WhisperBackend


@pytest.mark.skipif(os.environ.get("OPEN_KARAOKE_REAL_LYRICS") != "1", reason="Opt-in model test")
def test_real_whisper_checkpoint_and_inference(tmp_path):
    import torch

    torch.set_num_threads(2)
    name = os.environ.get("OPEN_KARAOKE_LYRIC_MODEL", "tiny")
    context = setup_audio(tmp_path, seconds=2)
    adapter = LyricsAdapter(lambda: WhisperBackend(name))
    options = adapter.validate_options({"language": "ja", "beam_size": 1})
    paths = adapter.analyze(context, options)
    lyrics, evidence, raw = [
        json.loads((tmp_path / path).read_text(encoding="utf-8")) for path in paths
    ]
    assert raw["inference_performed"] is True
    assert raw["model"]["id"] == f"whisper-{name}"
    assert raw["windows"][0]["result"]["language"] == "ja"
    assert evidence["items"][0]["evidence"]["model"] == raw["model"]
    for segment in lyrics["segments"]:
        assert 0 <= segment["start_sample"] < segment["end_sample"] <= 96000
    report = {
        "model": raw["model"],
        "duration_samples": raw["duration_samples"],
        "inference_performed": True,
        "artifact_count": len(paths),
        "candidate_count": len(lyrics["segments"]),
        "input_kind": "generated_tone",
        "singing_accuracy_verified": False,
        "quality_gate_applied": False,
    }
    directory = Path(os.environ.get("OPEN_KARAOKE_METRICS_DIR", "artifacts/lyrics"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"whisper-{name}.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report), flush=True)
