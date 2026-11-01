"""Hand-authored Standard MIDI File fixtures for the SMF importer (bytes built in-test)."""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.midi_import import MAX_EVENTS, import_smf
from open_karaoke_analysis.score_import import ImportedScore, ImporterRegistry, default_registry
from open_karaoke_analysis.song import _canonical


def vlq(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def on(channel: int, key: int, velocity: int = 100) -> bytes:
    return bytes([0x90 | channel, key, velocity])


def off(channel: int, key: int) -> bytes:
    return bytes([0x80 | channel, key, 64])


def meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + vlq(len(payload)) + payload


def tempo(us: int) -> bytes:
    return meta(0x51, us.to_bytes(3, "big"))


def name(text: str) -> bytes:
    return meta(0x03, text.encode("utf-8"))


def track(events: list[tuple[int, bytes]], *, end: bool = True) -> bytes:
    body = b"".join(vlq(delta) + event for delta, event in events)
    if end:
        body += b"\x00" + meta(0x2F, b"")
    return b"MTrk" + len(body).to_bytes(4, "big") + body


def smf(tracks: list[bytes], *, fmt: int | None = None, division: int = 480) -> bytes:
    fmt = (0 if len(tracks) == 1 else 1) if fmt is None else fmt
    header = fmt.to_bytes(2, "big") + len(tracks).to_bytes(2, "big") + division.to_bytes(2, "big")
    return b"MThd" + (6).to_bytes(4, "big") + header + b"".join(tracks)


def spans(result: ImportedScore) -> list[tuple[int, int, int]]:
    return [
        (row["start_sample"], row["end_sample"], row["midi_note"])
        for row in result.notes_document["notes"]
    ]


def expected_sample(changes: list[tuple[int, int]], ppq: int, tick: int) -> int:
    """Independent Fraction oracle: piecewise tempo integral rounded half up."""
    seconds = Fraction(0)
    points = [*changes, (tick, 0)]
    for (start, us), (stop, _) in zip(points, points[1:], strict=False):
        if start >= tick:
            break
        seconds += Fraction(min(stop, tick) - start, ppq) * Fraction(us, 1_000_000)
    exact = seconds * 48_000
    return int(exact + Fraction(1, 2)) if exact >= 0 else 0


def test_format0_default_tempo_and_velocity_zero_note_off() -> None:
    data = smf([track([(0, on(0, 60)), (480, off(0, 60)), (0, on(0, 62)), (480, on(0, 62, 0))])])
    result = import_smf(data)
    assert spans(result) == [(0, 24_000, 60), (24_000, 48_000, 62)]
    notes = result.notes_document
    assert notes["format_version"] == 1 and notes["analysis_version"] == 1
    assert [row["id"] for row in notes["notes"]] == ["midi-note-000001", "midi-note-000002"]
    assert all(row["pitch_confidence"] is None for row in notes["notes"])
    assert result.evidence_document["selection"]["reason"] == "single-candidate"
    # Output satisfies the existing canonical Song Data notes validation.
    assert len(_canonical(notes, "notes", 48_000, 1)) == 2


def test_tempo_change_in_conductor_track_is_exact() -> None:
    conductor = track([(0, tempo(500_000)), (960, tempo(250_000))])
    melody = track([(960, on(0, 64)), (480, off(0, 64))])
    result = import_smf(smf([conductor, melody]))
    assert spans(result) == [(48_000, 60_000, 64)]
    assert result.evidence_document["tempo_map"] == [
        {"tick": 0, "sample": 0, "us_per_quarter": 500_000},
        {"tick": 960, "sample": 48_000, "us_per_quarter": 250_000},
    ]


def test_non_integer_tick_rate_uses_exact_rational_math_without_drift() -> None:
    ppq = 7
    changes = [(0, 500_000), (5, 333_333), (20_003, 471_111)]
    conductor = track([(0, tempo(500_000)), (5, tempo(333_333)), (19_998, tempo(471_111))])
    starts = [1, 10, 7_000, 20_000, 20_010, 30_003]
    events: list[tuple[int, bytes]] = []
    previous = 0
    for start in starts:
        events.append((start - previous, on(0, 70)))
        events.append((1, off(0, 70)))
        previous = start + 1
    result = import_smf(smf([conductor, track(events)], division=ppq))
    got = spans(result)
    assert [row[0] for row in got] == [expected_sample(changes, ppq, tick) for tick in starts]
    assert [row[1] for row in got] == [expected_sample(changes, ppq, tick + 1) for tick in starts]


def test_smpte_division_converts_frames_to_samples() -> None:
    division = ((256 - 25) << 8) | 40  # 25 fps, 40 ticks/frame = 1000 ticks/s
    result = import_smf(smf([track([(1000, on(0, 60)), (500, off(0, 60))])], division=division))
    assert spans(result) == [(48_000, 72_000, 60)]
    drop = ((256 - 29) << 8) | 1  # 29.97 fps, 1 tick per frame
    result = import_smf(smf([track([(0, on(0, 60)), (30, off(0, 60))])], division=drop))
    assert spans(result) == [(0, 48_048, 60)]  # 30 * 1001/30000 s = 1.001 s


def test_multiple_tracks_select_melody_name_or_require_explicit_choice() -> None:
    piano = track([(0, name("Piano")), (0, on(0, 48)), (960, off(0, 48))])
    melody = track([(0, name("Lead Melody")), (0, on(1, 72)), (480, off(1, 72))])
    drums = track([(0, on(9, 36)), (100, off(9, 36))])
    result = import_smf(smf([track([]), piano, melody, drums]))
    assert spans(result) == [(0, 24_000, 72)]
    assert result.evidence_document["selection"] == {
        "track": 2,
        "channel": 1,
        "reason": "melody-track-name",
        "track_name": "Lead Melody",
    }

    anonymous = smf(
        [track([(0, on(0, 48)), (960, off(0, 48))]), track([(0, on(0, 72)), (480, off(0, 72))])]
    )
    with pytest.raises(ServiceError) as error:
        import_smf(anonymous)
    assert error.value.detail.code == "midi_track_ambiguous"
    explicit = import_smf(anonymous, {"track": 0})
    assert spans(explicit) == [(0, 48_000, 48)]
    assert explicit.evidence_document["selection"]["reason"] == "explicit"
    with pytest.raises(ServiceError) as error:
        import_smf(anonymous, {"track": 0, "channel": 3})
    assert error.value.detail.code == "midi_track_not_found"


def test_drum_channel_is_excluded_unless_requested() -> None:
    data = smf([track([(0, on(9, 36)), (100, off(9, 36))])])
    with pytest.raises(ServiceError) as error:
        import_smf(data)
    assert error.value.detail.code == "midi_no_notes"
    assert spans(import_smf(data, {"include_drum_channel": True})) == [(0, 5_000, 36)]


def test_overlaps_chords_and_duplicate_note_on_policy() -> None:
    events = [
        (0, on(0, 60)),
        (0, on(0, 64)),  # chord at same start: highest (64) kept
        (240, on(0, 67)),  # overlaps 64: 64 truncated to 240
        (240, off(0, 60)),
        (0, off(0, 64)),
        (0, on(0, 67)),  # duplicate note-on: previous 67 closes here (retrigger)
        (480, off(0, 67)),
    ]
    data = smf([track(events)])
    result = import_smf(data)
    assert spans(result) == [(0, 12_000, 64), (12_000, 24_000, 67), (24_000, 48_000, 67)]
    counts = result.evidence_document["counts"]
    assert counts["chord_dropped"] == 1
    assert counts["overlap_truncated"] == 1
    assert counts["retriggered"] == 1
    with pytest.raises(ServiceError) as error:
        import_smf(data, {"overlap": "error"})
    assert error.value.detail.code == "midi_overlap"


def test_same_tick_note_on_before_note_off_keeps_both_notes() -> None:
    # Some writers emit the next note-on before the previous note-off at the same tick.
    events = [(0, on(0, 60)), (480, on(0, 60)), (0, off(0, 60)), (480, off(0, 60))]
    result = import_smf(smf([track(events)]))
    assert spans(result) == [(0, 24_000, 60), (24_000, 48_000, 60)]
    zero = smf([track([(0, on(0, 60)), (0, off(0, 60)), (10, on(0, 61)), (10, off(0, 61))])])
    result = import_smf(zero)
    assert spans(result) == [(500, 1_000, 61)]
    assert result.evidence_document["counts"]["zero_length_dropped"] == 1


def test_sustain_pedal_policy() -> None:
    pedal_down, pedal_up = bytes([0xB0, 64, 127]), bytes([0xB0, 64, 0])
    events = [(0, pedal_down), (0, on(0, 60)), (240, off(0, 60)), (240, pedal_up)]
    data = smf([track(events)])
    assert spans(import_smf(data)) == [(0, 12_000, 60)]
    extended = import_smf(data, {"sustain": "extend"})
    assert spans(extended) == [(0, 24_000, 60)]
    assert extended.evidence_document["counts"]["sustain_extended"] == 1


def test_unterminated_notes_and_missing_end_of_track_close_at_track_end() -> None:
    data = smf([track([(0, on(0, 60)), (480, bytes([0xB0, 7, 100]))], end=False)])
    result = import_smf(data)
    assert spans(result) == [(0, 24_000, 60)]
    assert result.evidence_document["counts"]["unterminated"] == 1
    assert result.evidence_document["counts"]["tracks_missing_end_of_track"] == 1


def test_running_status_unknown_chunks_and_sysex_are_handled() -> None:
    events = [
        (0, bytes([0xF0, 2, 0x7E, 0xF7])),
        (0, on(0, 60)),
        (480, bytes([60, 0])),  # running status note-on velocity 0 = note-off
    ]
    data = smf([track(events)])
    alien = b"XFIH" + (3).to_bytes(4, "big") + b"abc"
    data = data[:14] + alien + data[14:]
    assert spans(import_smf(data)) == [(0, 24_000, 60)]


def test_offset_transpose_and_song_duration_bounds() -> None:
    data = smf([track([(0, on(0, 60)), (480, off(0, 60))])])
    result = import_smf(data, {"offset_samples": 1_000, "transpose_semitones": -12})
    assert spans(result) == [(1_000, 25_000, 48)]
    for options, code in [
        ({"offset_samples": -1}, "midi_negative_time"),
        ({"transpose_semitones": 70}, "midi_invalid_pitch"),
        ({"duration_samples": 23_999}, "midi_exceeds_song_duration"),
    ]:
        with pytest.raises(ServiceError) as error:
            import_smf(data, options)
        assert error.value.detail.code == code


def test_lyric_meta_events_are_kept_as_evidence_with_strict_encoding() -> None:
    lyric = "あ".encode("cp932")
    data = smf([track([(0, on(0, 60)), (0, meta(0x05, lyric)), (480, off(0, 60))])])
    result = import_smf(data, {"text_encoding": "cp932"})
    assert result.evidence_document["lyric_events"] == [
        {"track": 0, "source_sample": 0, "text": "あ"}
    ]
    with pytest.raises(ServiceError) as error:
        import_smf(data)
    assert error.value.detail.code == "midi_invalid_text_encoding"


def test_time_signature_is_recorded_without_changing_timing() -> None:
    timesig = meta(0x58, bytes([7, 3, 24, 8]))  # 7/8
    data = smf([track([(0, timesig), (0, on(0, 60)), (480, off(0, 60))])])
    result = import_smf(data)
    assert spans(result) == [(0, 24_000, 60)]
    assert result.evidence_document["time_signatures"] == [
        {"tick": 0, "sample": 0, "numerator": 7, "denominator": 8}
    ]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"RIFF" + bytes(10), "midi_malformed"),
        (b"MThd\x00\x00\x00\x06\x00", "midi_malformed"),
        (smf([track([])], fmt=2), "midi_unsupported_format"),
        (smf([track([]), track([])], fmt=0), "midi_malformed"),
        (smf([track([])])[:-2], "midi_malformed"),
        (smf([track([])], division=0), "midi_malformed"),
        (smf([track([(0, tempo(0))])]), "midi_malformed"),
        (smf([track([(0, bytes([0x90, 60]))], end=False)]), "midi_malformed"),
        (smf([track([(0, bytes([0x90, 0x80, 1]))])]), "midi_malformed"),
        (smf([track([(0, bytes([60, 100]))])]), "midi_malformed"),
        (smf([track([(0, bytes([0xF8]))])]), "midi_malformed"),
        (smf([track([(0, bytes([0xFF, 0x05, 0x8F, 0x8F, 0x8F, 0x8F, 0x01]))])]), "midi_malformed"),
        (smf([track([(0, meta(0x58, bytes([4, 9, 24, 8])))])]), "midi_malformed"),
        (smf([track([]) + b"\x00\x90\x3c\x40"]), "midi_malformed"),
        (smf([track([(0, on(0, 60))]) + b"MTrk\x00\x00\x00\x00"]), "midi_malformed"),
        (smf([track([(0, meta(0x2F, b"")), (0, on(0, 60))], end=False)]), "midi_malformed"),
        (smf([track([])]), "midi_no_notes"),
    ],
)
def test_malformed_and_unsupported_files_fail_with_distinct_codes(data: bytes, code: str) -> None:
    with pytest.raises(ServiceError) as error:
        import_smf(data)
    assert error.value.detail.code == code


def test_event_and_size_limits_are_enforced() -> None:
    many = b"".join(b"\x00" + bytes([0xB0, 7, 1]) for _ in range(MAX_EVENTS + 1))
    data = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0" + b"MTrk"
    data += len(many).to_bytes(4, "big") + many
    with pytest.raises(ServiceError) as error:
        import_smf(data)
    assert error.value.detail.code == "midi_too_many_events"
    with pytest.raises(ServiceError) as error:
        import_smf(b"MThd" + bytes(16 * 1024 * 1024))
    assert error.value.detail.code == "midi_too_large"


@pytest.mark.parametrize(
    "options",
    [{"track": True}, {"unknown": 1}, {"sustain": "maybe"}, {"note_id_prefix": "bad id"}],
)
def test_invalid_options_are_rejected(options: dict) -> None:
    data = smf([track([(0, on(0, 60)), (480, off(0, 60))])])
    with pytest.raises(ServiceError) as error:
        import_smf(data, options)
    assert error.value.detail.code == "invalid_options"


def test_import_is_deterministic() -> None:
    conductor = track([(0, tempo(600_000)), (100, tempo(400_000))])
    melody = track([(0, name("vocal")), (3, on(2, 65)), (200, off(2, 65)), (1, on(2, 67))])
    data = smf([conductor, melody, track([(0, on(3, 40)), (5, off(3, 40))])])
    first = import_smf(data)
    second = import_smf(bytearray(data))
    assert json.dumps(first.notes_document, sort_keys=True) == json.dumps(
        second.notes_document, sort_keys=True
    )
    assert first.evidence_document == second.evidence_document
    assert len(first.evidence_document["source_sha256"]) == 64


def test_registry_detects_formats_and_rejects_unknown_or_duplicate() -> None:
    registry = default_registry()
    assert registry.formats() == ["smf"]
    data = smf([track([(0, on(0, 60)), (480, off(0, 60))])])
    assert spans(registry.import_score(data)) == [(0, 24_000, 60)]
    assert spans(registry.import_score(data, {}, format_id="smf")) == [(0, 24_000, 60)]
    for kwargs, code in [
        ({"format_id": "kar-xyz"}, "score_import_unknown_format"),
        ({}, "score_import_unknown_format"),
    ]:
        with pytest.raises(ServiceError) as error:
            registry.import_score(b"" if not kwargs else data, None, **kwargs)
        assert error.value.detail.code == code
    with pytest.raises(ValueError):
        registry.register(registry._importers["smf"])
    assert ImporterRegistry().formats() == []
