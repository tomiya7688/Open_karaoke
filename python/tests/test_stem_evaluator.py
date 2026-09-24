import json
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.stem_evaluator import evaluate_arrays, evaluate_files, si_sdr


def fixture():
    t = np.arange(4801) / 48000
    v = np.stack([np.sin(2 * np.pi * 220 * t)] * 2, axis=1)
    b = np.stack([np.sin(2 * np.pi * 660 * t)] * 2, axis=1) * 0.5
    return v, b


def test_quality_metric_known_pass_fail_and_scale_invariance():
    v, b = fixture()
    assert si_sdr(v, 2 * v + 0.2 * b) == pytest.approx(si_sdr(v, v + 0.1 * b), abs=1e-8)
    good = evaluate_arrays(v, b, v, b, min_vocals_si_sdr=30)
    bad = evaluate_arrays(v, b, v + b, np.zeros_like(b), min_vocals_si_sdr=30)
    assert good["passed"] and not bad["passed"]
    assert good["quality_gate_applied"]
    assert good["metrics"]["vocals"]["si_sdr_db"] > bad["metrics"]["vocals"]["si_sdr_db"]
    assert good["dataset_sha256"] == bad["dataset_sha256"]
    json.dumps(good, allow_nan=False)


def test_silent_references_are_undefined_and_ungated_is_explicit():
    silence = np.zeros((100, 2))
    result = evaluate_arrays(silence, silence, silence, silence)
    assert result["passed"] and not result["quality_gate_applied"]
    assert result["metrics"]["vocals"]["si_sdr_db"] is None
    json.dumps(result, allow_nan=False)
    assert not evaluate_arrays(silence, silence, silence, silence, min_vocals_si_sdr=0)["passed"]


def test_malformed_shapes_samples_and_thresholds():
    v, b = fixture()
    for wrong in [v[:-1], np.full_like(v, np.nan), np.empty((0, 2))]:
        with pytest.raises(ValueError):
            evaluate_arrays(v, b, wrong, b)
    with pytest.raises(ValueError):
        evaluate_arrays(v, b, v, b, min_vocals_si_sdr=float("nan"))
    assert si_sdr(v, np.zeros_like(v)) == -120


def test_file_evaluator_and_cli(tmp_path):
    v, b = fixture()
    paths = [tmp_path / name for name in ["rv.wav", "rb.wav", "v.wav", "b.wav"]]
    for path, audio in zip(paths, [v, b, v, b], strict=True):
        sf.write(path, audio, 48000, subtype="FLOAT")
    output = tmp_path / "metrics.json"
    command = [
        sys.executable,
        "-m",
        "open_karaoke_analysis.stem_evaluator",
        "--reference-vocals",
        str(paths[0]),
        "--reference-accompaniment",
        str(paths[1]),
        "--vocals",
        str(paths[2]),
        "--accompaniment",
        str(paths[3]),
        "--output",
        str(output),
        "--dataset-version",
        "test-v1",
        "--min-vocals-si-sdr",
        "30",
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=20)
    assert json.loads(output.read_text())["passed"]
    sf.write(paths[2], v + b, 48000, subtype="FLOAT")
    assert subprocess.run(command, capture_output=True, timeout=20).returncode == 1
    sf.write(paths[2], v, 44100, subtype="FLOAT")
    with pytest.raises(ValueError):
        evaluate_files(paths)
