"""Test-only export producer for actual Rust audio-inclusive SongStore imports."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import soundfile as sf
from test_song import FixtureStage, context

from open_karaoke_analysis.adapters import AdapterRegistry
from open_karaoke_analysis.notes import NotesAdapter
from open_karaoke_analysis.pitch import PitchAdapter
from open_karaoke_analysis.song import SongAdapter
from open_karaoke_analysis.vocal_events import VocalEventAdapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    root.mkdir(parents=True, exist_ok=True)
    source = root / "import-tone.wav"
    mono = 0.2 * np.sin(2 * np.pi * 440 * np.arange(9600) / 48_000)
    sf.write(source, np.repeat(mono[:, None], 2, axis=1), 48_000, subtype="FLOAT")
    registry = AdapterRegistry(
        [FixtureStage(role, []) for role in ("stems", "lyrics", "alignment")]
        + [PitchAdapter(), VocalEventAdapter(), NotesAdapter()]
    )
    artifacts = SongAdapter(registry).analyze(
        context(root, source), {"title": "合成・全曲登録", "analysis_version": 2}
    )
    now = datetime.now(UTC).isoformat()
    job = {
        "id": str(uuid4()),
        "kind": "analysis.song",
        "status": "completed",
        "progress": 1.0,
        "stage": "completed",
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "error": None,
        "artifacts": artifacts,
    }
    (root / "import-job.json").write_text(
        json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
