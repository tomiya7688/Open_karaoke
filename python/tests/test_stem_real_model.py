"""Opt-in real CPU weights test; explicitly run by stem-model.yml, never a mock."""

import json
import os
from pathlib import Path
from threading import Event

import numpy as np
import pytest
import soundfile as sf
from stem_fixtures import DATASET_VERSION, reference_stems

from open_karaoke_analysis.adapters import AnalysisContext, atomic_json
from open_karaoke_analysis.stem_backend import WEIGHT_SHA256
from open_karaoke_analysis.stem_evaluator import evaluate_arrays
from open_karaoke_analysis.stems import StemsAdapter


@pytest.mark.skipif(
    os.environ.get("OPEN_KARAOKE_REAL_STEMS") != "1", reason="Opt-in pinned weights test"
)
def test_real_weights_separation_and_cache(tmp_path):
    import torch

    torch.set_num_threads(2)
    v_ref, b_ref = reference_stems()
    mix = v_ref + b_ref
    sf.write(tmp_path / "input.wav", mix, 48000, subtype="FLOAT")
    adapter = StemsAdapter()
    all_results = []
    for job in ("first", "cached"):
        context = AnalysisContext(
            tmp_path,
            f"jobs/real/{job}",
            tmp_path / "input.wav",
            Event(),
            lambda progress, stage: print(stage, round(progress, 3), flush=True),
        )
        paths = adapter.analyze(context, adapter.validate_options({}))
        meta = json.loads((tmp_path / paths[-1]).read_text())
        vocal, rate = sf.read(tmp_path / paths[0], dtype="float32", always_2d=True)
        backing, _ = sf.read(tmp_path / paths[1], dtype="float32", always_2d=True)
        assert rate == 48000 and vocal.shape == backing.shape == mix.shape
        assert meta["model"]["weights_sha256"] == WEIGHT_SHA256
        assert meta["model"]["weights_license"] == "MIT"
        assert meta["cache_hit"] == (job == "cached")
        assert np.isfinite(vocal).all() and np.isfinite(backing).all()
        assert np.max(np.abs(vocal)) > 1e-5
        assert not np.allclose(vocal, mix)
        np.testing.assert_allclose(vocal + backing, mix, atol=6e-8)
        all_results.append((vocal, backing))
    np.testing.assert_array_equal(all_results[0][0], all_results[1][0])
    report = evaluate_arrays(v_ref, b_ref, *all_results[0], dataset_version=DATASET_VERSION)
    report["model"] = meta["model"]
    report["note"] = "Synthetic integration fixture only; no real-song quality gate calibrated yet."
    report_dir = Path(os.environ.get("OPEN_KARAOKE_METRICS_DIR", str(tmp_path / "metrics")))
    atomic_json(report_dir / "real-stem-metrics.json", report)
    print(json.dumps(report, allow_nan=False), flush=True)
