"""Structural regression: independent adapters share contracts by delegation, not inheritance."""

import sys
from collections.abc import Callable

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from open_karaoke_analysis.adapters import AdapterRegistry, MockAdapter, validate_adapter_options
from open_karaoke_analysis.alignment import AlignmentAdapter
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.lyrics import LyricsAdapter
from open_karaoke_analysis.notes import NotesAdapter
from open_karaoke_analysis.pitch import PitchAdapter
from open_karaoke_analysis.song import SongAdapter
from open_karaoke_analysis.stems import StemsAdapter
from open_karaoke_analysis.vocal_events import VocalEventAdapter


ADAPTER_CASES = [
    (MockAdapter, {}, "Invalid mock options"),
    (LyricsAdapter, {}, "Invalid lyric transcription options"),
    (AlignmentAdapter, {"lyrics_artifact": "lyrics.json"}, "Invalid lyric alignment options"),
    (PitchAdapter, {}, "Invalid pitch-analysis options"),
    (VocalEventAdapter, {"pitch_artifact": "pitch.json"}, "Invalid Vocal Event options"),
    (StemsAdapter, {}, "Invalid stem separation options"),
    (
        NotesAdapter,
        {"pitch_artifact": "pitch.json", "vocal_event_artifact": "vocal_events.json"},
        "Invalid score-generation options",
    ),
    (lambda: SongAdapter(AdapterRegistry([])), {"title": "A song"}, "Invalid full-song options"),
]


@pytest.mark.parametrize(("factory", "valid_options", "message"), ADAPTER_CASES)
def test_each_adapter_delegates_to_the_shared_validator(
    monkeypatch, factory: Callable, valid_options: dict, message: str
):
    adapter = factory()
    calls = []

    def spy(schema, options, text):
        calls.append((schema, options, text))
        return {"validated": True}

    # アダプタ自身が持つ検証を使わず、共有関数へ自分のスキーマと固有のメッセージで委譲していること。
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "validate_adapter_options", spy)
    assert adapter.validate_options(valid_options) == {"validated": True}
    assert len(calls) == 1
    schema, options, text = calls[0]
    assert issubclass(schema, BaseModel) and options == valid_options and text == message


@pytest.mark.parametrize(("factory", "valid_options", "message"), ADAPTER_CASES)
def test_missing_required_options_and_non_dict_input_are_invalid_options(
    factory: Callable, valid_options: dict, message: str
):
    adapter = factory()
    inputs = [{"unexpected_property": True}, None, [], "text"]
    if valid_options:  # 必須項目を持つアダプタは、空の設定も拒否する
        inputs.append({})
    for bad in inputs:
        with pytest.raises(ServiceError) as rejected:
            adapter.validate_options(bad)
        assert rejected.value.detail.code == "invalid_options"
        assert rejected.value.detail.message == message


@pytest.mark.parametrize(("factory", "valid_options", "message"), ADAPTER_CASES)
def test_adapter_validation_is_consistent_without_concrete_inheritance(
    factory: Callable, valid_options: dict, message: str
):
    adapter = factory()
    # ModelAdapter is structural (Protocol); product adapters must not inherit a
    # behavior-bearing product class. Framework/typing bases would be allowed.
    product_bases = [
        base
        for base in type(adapter).__mro__[1:]
        if base.__module__.startswith("open_karaoke_analysis")
    ]
    assert product_bases == []
    assert adapter.info.id
    assert isinstance(adapter.validate_options(valid_options), dict)

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_options({**valid_options, "unexpected_property": True})
    assert rejected.value.detail.code == "invalid_options"
    assert rejected.value.detail.message == message


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int = 1


def test_validate_adapter_options_returns_a_validated_copy_and_chains_the_cause():
    options = {"count": 3}
    validated = validate_adapter_options(_Schema, options, "Invalid test options")
    assert validated == {"count": 3} and validated is not options
    assert validate_adapter_options(_Schema, {}, "Invalid test options") == {"count": 1}

    for bad in ({"count": "3"}, {"count": 1, "extra": True}):
        with pytest.raises(ServiceError) as rejected:
            validate_adapter_options(_Schema, bad, "Invalid test options")
        assert rejected.value.detail.code == "invalid_options"
        assert rejected.value.detail.message == "Invalid test options"
        assert isinstance(rejected.value.__cause__, ValidationError)


def test_shared_option_validation_keeps_schema_specific_errors():
    with pytest.raises(ServiceError) as rejected:
        PitchAdapter().validate_options({"mode": "single", "detectors": ["yin", "nacf"]})
    assert rejected.value.detail.code == "invalid_options"

    with pytest.raises(ServiceError) as rejected:
        SongAdapter(AdapterRegistry([])).validate_options({"title": "  "})
    assert rejected.value.detail.code == "invalid_options"
    assert rejected.value.detail.message == "Invalid full-song options"
