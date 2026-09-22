"""Real pretrained acoustic-model smoke, NOT a Japanese singing-accuracy test."""

import json
import os
from pathlib import Path
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AnalysisContext, atomic_json
from open_karaoke_analysis.alignment import AlignmentAdapter
from open_karaoke_analysis.alignment_backend import WEIGHT_SHA256


@pytest.mark.skipif(
    os.environ.get("OPEN_KARAOKE_REAL_ALIGNMENT") != "1",
    reason="Opt-in Japanese acoustic checkpoint download",
)
def test_actual_japanese_acoustic_model(tmp_path):
    import torch

    torch.set_num_threads(2)
    signal = (0.1 * np.sin(2 * np.pi * 220 * np.arange(48000) / 48000)).astype(np.float32)
    sf.write(tmp_path / "vocals.wav", signal, 48000, subtype="FLOAT")
    atomic_json(
        tmp_path / "lyrics.json",
        {
            "format_version": 1,
            "analysis_version": 1,
            "segments": [{"id": "smoke", "text": "あ", "start_sample": 0, "end_sample": 48000}],
        },
    )
    context = AnalysisContext(
        tmp_path, "jobs/real/alignment", tmp_path / "vocals.wav", Event(), lambda *_: None
    )
    adapter = AlignmentAdapter()
    # No acoustic-quality gate on an intentionally non-singing input.
    options = adapter.validate_options({"lyrics_artifact": "lyrics.json", "min_token_score": 0.0})
    paths = adapter.analyze(context, options)
    report = json.loads((tmp_path / paths[1]).read_text(encoding="utf-8"))
    assert report["inference_performed"] is True
    assert report["model"]["weights_sha256"] == WEIGHT_SHA256
    assert report["lines"][0]["status"] == "aligned"
    assert 0 <= report["lines"][0]["start_sample"] < report["lines"][0]["end_sample"] <= 48000
    assert len(paths) == 3
    directory = Path(os.environ.get("OPEN_KARAOKE_METRICS_DIR", "artifacts/alignment"))
    atomic_json(
        directory / "real-alignment-smoke.json",
        {
            "model": report["model"],
            "inference_performed": True,
            "fixture": "generated-220hz-tone-v1",
            "input_sha256": report["input_sha256"],
            "artifacts": [Path(path).name for path in paths],
            "singing_accuracy_verified": False,
            "quality_gate_applied": False,
            "note": "Tone input tests integration, not Japanese singing accuracy.",
        },
    )
