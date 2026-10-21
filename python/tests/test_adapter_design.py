"""Structural regression: independent adapters share contracts by delegation, not inheritance."""

from collections.abc import Callable

import pytest

from open_karaoke_analysis.adapters import AdapterRegistry, MockAdapter
from open_karaoke_analysis.alignment import AlignmentAdapter
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.lyrics import LyricsAdapter
from open_karaoke_analysis.notes import NotesAdapter
from open_karaoke_analysis.pitch import PitchAdapter
from open_karaoke_analysis.song import SongAdapter
from open_karaoke_analysis.stems import StemsAdapter
from open_karaoke_analysis.vocal_events import VocalEventAdapter


@pytest.mark.parametrize(
    ("factory", "valid_options", "message"),
    [
        (MockAdapter, {}, "Invalid mock options"),
        (LyricsAdapter, {}, "Invalid lyric transcription options"),
        (AlignmentAdapter, {"lyrics_artifact": "lyrics.json"}, "Invalid lyric alignment options"),
        (PitchAdapter, {}, "Invalid pitch-analysis options"),
        (
            VocalEventAdapter,
            {"pitch_artifact": "pitch.json"},
            "Invalid Vocal Event options",
        ),
        (StemsAdapter, {}, "Invalid stem separation options"),
        (
            NotesAdapter,
            {"pitch_artifact": "pitch.json", "vocal_event_artifact": "vocal_events.json"},
            "Invalid score-generation options",
        ),
        (
            lambda: SongAdapter(AdapterRegistry([])),
            {"title": "A song"},
            "Invalid full-song options",
        ),
    ],
)
def test_adapter_validation_is_consistent_without_concrete_inheritance(
    factory: Callable, valid_options: dict, message: str
):
    adapter = factory()
    # ModelAdapter is structural (Protocol); product adapters must not
    # inherit a behavior-bearing base adapter.
    assert type(adapter).__bases__ == (object,)
    assert adapter.info.id
    assert isinstance(adapter.validate_options(valid_options), dict)

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_options({**valid_options, "unexpected_property": True})
    assert rejected.value.detail.code == "invalid_options"
    assert rejected.value.detail.message == message


def test_shared_option_validation_keeps_schema_specific_errors():
    with pytest.raises(ServiceError) as rejected:
        PitchAdapter().validate_options({"mode": "single", "detectors": ["yin", "nacf"]})
    assert rejected.value.detail.code == "invalid_options"

    with pytest.raises(ServiceError) as rejected:
        SongAdapter(AdapterRegistry([])).validate_options({"title": "  "})
    assert rejected.value.detail.code == "invalid_options"
    assert rejected.value.detail.message == "Invalid full-song options"
