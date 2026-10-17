"""Generate a test-only pipeline result for the Rust SongBundle contract test."""

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from test_song import context, fixture_registry

from open_karaoke_analysis.song import SongAdapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    root.mkdir(parents=True, exist_ok=True)
    source = root / "input.wav"
    mono = 0.2 * np.sin(2 * np.pi * 440 * np.arange(9600) / 48_000)
    sf.write(source, np.repeat(mono[:, None], 2, axis=1), 48_000, subtype="FLOAT")
    artifacts = SongAdapter(fixture_registry([])).analyze(
        context(root, source), {"title": "合成fixture", "analysis_version": 2}
    )
    result = json.loads((root / artifacts[-1]).read_text(encoding="utf-8"))
    (root / "bundle.json").write_text(
        json.dumps(result["bundle"], ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
