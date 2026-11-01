"""Self-contained Standard MIDI File (format 0/1) importer for canonical scoring notes.

Policies are specified in docs/midi_import.md. All time conversion is exact integer math.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .adapters import validate_adapter_options
from .contracts import ServiceError
from .score_import import MAX_IMPORT_BYTES, ImportedScore

RATE = 48_000
# Same upper bound as the song pipeline (song.MAX_SAMPLES): 30 minutes at 48 kHz.
MAX_DURATION_SAMPLES = 30 * 60 * RATE
MAX_TRACKS = 256
MAX_EVENTS = 500_000
MAX_NOTES = 100_000
MAX_LYRIC_EVENTS = 100_000
DEFAULT_TEMPO_US = 500_000
MAX_VLQ = 0x0FFFFFFF
DRUM_CHANNEL = 9
SUSTAIN_CC = 64
MELODY_KEYWORDS = ("melody", "vocal", "voice", "lead", "メロ", "ボーカル", "歌")
IMPORTER_ID = "smf-importer-v1"
IMPORTER_VERSION = "1.0.0"
POLICY_VERSION = 1


class MidiImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    track: int | None = Field(default=None, ge=0, lt=MAX_TRACKS)
    channel: int | None = Field(default=None, ge=0, le=15)
    include_drum_channel: bool = False
    sustain: Literal["ignore", "extend"] = "ignore"
    overlap: Literal["truncate", "error"] = "truncate"
    offset_samples: int = Field(default=0, ge=-MAX_DURATION_SAMPLES, le=MAX_DURATION_SAMPLES)
    transpose_semitones: int = Field(default=0, ge=-127, le=127)
    duration_samples: int | None = Field(default=None, ge=1, le=MAX_DURATION_SAMPLES)
    text_encoding: Literal["utf-8", "cp932", "latin-1"] = "utf-8"
    analysis_version: int = Field(default=1, ge=1, le=2_147_483_647)
    note_id_prefix: str = Field(default="midi-note", pattern=r"^[A-Za-z0-9_-]{1,32}$")


MidiImportOptions.__doc__ = """
{
  責務: [MidiImportOptions: SMF取込の利用者設定を厳密に検証する]
  フィールド: [
    track/channel: 採点melodyとして使うtrack番号・channel（任意）,
    include_drum_channel: channel 10(0始まり9)を候補に含めるか,
    sustain: CC64を無視するか、note終了をpedal解放まで延長するか,
    overlap: 単音化での重なりを切り詰めるか、エラーにするか,
    offset_samples/transpose_semitones: 音源との相対オフセット・キー差,
    duration_samples: 曲の長さ（指定時は超過をエラー）,
    text_encoding: track名・歌詞metaの文字コード,
    analysis_version/note_id_prefix: 出力notes.jsonのversionとID接頭辞
  ]
}
"""


def _fail(code: str, message: str) -> ServiceError:
    """
    {
      責務: [_fail: MIDI取込の判別可能なServiceErrorを作る]
      処理: [1: codeとmessageを包む]
      引数: [code: エラーcode, message: 処理段階と原因]
      戻り値: [ServiceError]
    }
    """
    return ServiceError(code, message)


@dataclass
class _Track:
    """
    {
      責務: [_Track: 1つのMTrk chunkから読んだ有界なイベント列を保持する]
      フィールド: [
        index: file内のMTrk順序, name: track名bytes, events: (tick, seq, kind, channel, a, b),
        tempos: (tick, seq, us_per_quarter), time_signatures: (tick, num, den, clocks, n32),
        lyrics: (tick, seq, bytes), end_tick: 最後のイベントtick, has_end_of_track: EOT有無
      ]
    }
    """

    index: int
    name: bytes | None = None
    events: list[tuple[int, int, str, int, int, int]] = field(default_factory=list)
    tempos: list[tuple[int, int, int]] = field(default_factory=list)
    time_signatures: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    lyrics: list[tuple[int, int, bytes]] = field(default_factory=list)
    end_tick: int = 0
    has_end_of_track: bool = False


class _Reader:
    """
    {
      責務: [_Reader: bytesを範囲内で読み、超過や不正値をmidi_malformedへ変換する]
      フィールド: [data: 入力bytes, pos: 現在位置, end: 読取上限]
    }
    """

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        """
        {
          責務: [__init__: 読取範囲を設定する]
          処理: [1: 位置と上限を保存する]
          引数: [data: bytes, pos: 開始位置, end: 終了位置(排他的)]
          戻り値: []
        }
        """
        self.data = data
        self.pos = pos
        self.end = end

    def take(self, count: int, what: str) -> bytes:
        """
        {
          責務: [take: 範囲内のcount bytesを読む]
          処理: [1: 残量を確認する, 2: 読んで位置を進める]
          引数: [count: 読むbytes数, what: エラー用の対象名]
          戻り値: [bytes]
          エラー: [midi_malformed: 範囲を超える]
        }
        """
        if count < 0 or self.pos + count > self.end:
            raise _fail("midi_malformed", f"Truncated {what} at byte {self.pos}")
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def u8(self, what: str) -> int:
        """
        {
          責務: [u8: 1 byteを読む]
          処理: [1: takeで1 byte読む]
          引数: [what: エラー用の対象名]
          戻り値: [0..255]
        }
        """
        return self.take(1, what)[0]

    def vlq(self, what: str) -> int:
        """
        {
          責務: [vlq: SMF可変長数値(最大4 bytes)を読む]
          処理: [1: 継続bitを最大4回まで読む, 2: 5 bytes目が必要なら拒否する]
          引数: [what: エラー用の対象名]
          戻り値: [0..0x0FFFFFFF]
          エラー: [midi_malformed: 4 bytesを超える、または途中で切れる]
        }
        """
        value = 0
        for _ in range(4):
            byte = self.u8(what)
            value = (value << 7) | (byte & 0x7F)
            if byte < 0x80:
                return value
        raise _fail("midi_malformed", f"Variable-length {what} exceeds 4 bytes at {self.pos}")


def _parse_track(data: bytes, start: int, end: int, index: int, budget: list[int]) -> _Track:
    """
    {
      責務: [_parse_track: 1つのMTrk本文を有界に解析する]
      処理: [
        1: delta timeとstatus(running status含む)を読む,
        2: channel/sysex/meta eventを分類し、必要なものだけ保持する,
        3: End of Track後のデータを拒否する
      ]
      引数: [data: file bytes, start/end: chunk本文範囲, index: track番号, budget: 残りevent数]
      戻り値: [_Track]
      エラー: [midi_malformed: 構文違反, midi_too_many_events: event上限超過]
    }
    """
    reader = _Reader(data, start, end)
    track = _Track(index=index)
    tick = 0
    running: int | None = None
    seq = 0
    # Each iteration consumes at least one byte, so the loop is bounded by the chunk length.
    while reader.pos < reader.end:
        if track.has_end_of_track:
            raise _fail("midi_malformed", f"Track {index} has data after End of Track")
        budget[0] -= 1
        if budget[0] < 0:
            raise _fail("midi_too_many_events", f"MIDI file exceeds {MAX_EVENTS} events")
        tick += reader.vlq("delta time")
        seq += 1
        status = data[reader.pos] if reader.pos < reader.end else -1
        if status < 0:
            raise _fail("midi_malformed", f"Track {index} ends inside an event")
        if status >= 0x80:
            reader.pos += 1
        elif running is None:
            raise _fail("midi_malformed", f"Track {index} uses running status without a status")
        else:
            status = running
        if 0x80 <= status <= 0xEF:
            running = status
            kind = status & 0xF0
            first = reader.u8("channel data")
            second = 0 if kind in (0xC0, 0xD0) else reader.u8("channel data")
            if first >= 0x80 or second >= 0x80:
                raise _fail("midi_malformed", f"Track {index} has an invalid data byte")
            channel = status & 0x0F
            if kind == 0x90 and second > 0:
                track.events.append((tick, seq, "on", channel, first, second))
            elif kind in (0x80, 0x90):
                track.events.append((tick, seq, "off", channel, first, second))
            elif kind == 0xB0 and first == SUSTAIN_CC:
                track.events.append((tick, seq, "sustain", channel, second, 0))
        elif status in (0xF0, 0xF7):
            running = None
            reader.take(reader.vlq("sysex length"), "sysex data")
        elif status == 0xFF:
            running = None
            meta = reader.u8("meta type")
            payload = reader.take(reader.vlq("meta length"), "meta data")
            if meta == 0x2F:
                if payload:
                    raise _fail("midi_malformed", f"Track {index} has a non-empty End of Track")
                track.has_end_of_track = True
            elif meta == 0x51:
                if len(payload) != 3:
                    raise _fail("midi_malformed", f"Track {index} has an invalid tempo event")
                tempo = int.from_bytes(payload, "big")
                if tempo == 0:
                    raise _fail("midi_malformed", f"Track {index} has a zero tempo")
                track.tempos.append((tick, seq, tempo))
            elif meta == 0x58:
                if len(payload) != 4 or payload[0] == 0 or payload[1] > 7:
                    raise _fail(
                        "midi_malformed", f"Track {index} has an invalid time signature event"
                    )
                track.time_signatures.append(
                    (tick, payload[0], 1 << payload[1], payload[2], payload[3])
                )
            elif meta == 0x03 and track.name is None:
                track.name = payload
            elif meta == 0x05:
                track.lyrics.append((tick, seq, payload))
        else:
            raise _fail(
                "midi_malformed", f"Track {index} has unsupported status byte 0x{status:02X}"
            )
        track.end_tick = tick
    return track


@dataclass(frozen=True)
class _Header:
    """
    {
      責務: [_Header: MThdの形式とtime divisionを保持する]
      フィールド: [format: 0または1, track_count: MTrk数, division: 生のdivision値]
    }
    """

    format: int
    track_count: int
    division: int


def _parse(data: bytes) -> tuple[_Header, list[_Track]]:
    """
    {
      責務: [_parse: SMF全体をheaderとtrack列へ解析する]
      処理: [
        1: MThdを検証する, 2: chunkを順に読み、未知chunkは読み飛ばす,
        3: MTrk数がheaderと一致するか検証する
      ]
      引数: [data: file bytes]
      戻り値: [(_Header, tracks)]
      エラー: [midi_malformed, midi_unsupported_format, midi_too_many_tracks]
    }
    """
    reader = _Reader(data, 0, len(data))
    if reader.take(4, "header magic") != b"MThd":
        raise _fail("midi_malformed", "Missing MThd header")
    length = int.from_bytes(reader.take(4, "header length"), "big")
    if length < 6:
        raise _fail("midi_malformed", "MThd header is shorter than 6 bytes")
    body = _Reader(data, reader.pos, reader.pos + length)
    if body.end > len(data):
        raise _fail("midi_malformed", "Truncated MThd header")
    fmt = int.from_bytes(body.take(2, "format"), "big")
    count = int.from_bytes(body.take(2, "track count"), "big")
    division = int.from_bytes(body.take(2, "division"), "big")
    reader.pos = body.end
    if fmt not in (0, 1):
        raise _fail("midi_unsupported_format", f"SMF format {fmt} is not supported")
    if count == 0 or (fmt == 0 and count != 1):
        raise _fail("midi_malformed", f"SMF format {fmt} cannot have {count} tracks")
    if count > MAX_TRACKS:
        raise _fail("midi_too_many_tracks", f"MIDI file exceeds {MAX_TRACKS} tracks")
    if division == 0:
        raise _fail("midi_malformed", "Ticks per quarter note must be positive")
    if division & 0x8000:
        fps = 256 - (division >> 8)
        if fps not in (24, 25, 29, 30) or division & 0xFF == 0:
            raise _fail("midi_malformed", "Invalid SMPTE time division")
    header = _Header(fmt, count, division)
    tracks: list[_Track] = []
    budget = [MAX_EVENTS]
    # Each chunk consumes at least its 8-byte header, bounding the loop by the file size.
    while reader.pos < reader.end:
        kind = reader.take(4, "chunk type")
        size = int.from_bytes(reader.take(4, "chunk length"), "big")
        start = reader.pos
        reader.take(size, "chunk body")
        if kind != b"MTrk":
            continue
        if len(tracks) >= count:
            raise _fail("midi_malformed", "MIDI file has more MTrk chunks than its header")
        tracks.append(_parse_track(data, start, start + size, len(tracks), budget))
    if len(tracks) != count:
        raise _fail("midi_malformed", f"Header declares {count} tracks, found {len(tracks)}")
    return header, tracks


class _TempoMap:
    """
    {
      責務: [_TempoMap: tickを48 kHz整数sample indexへ厳密な有理数計算で変換する]
      フィールド: [
        ticks: tempo区間の開始tick, numerators: 区間開始までの累積分子,
        rates: 区間の1 tickあたり分子, changes: 採用した(tick, us/quarter), denominator: 共通分母,
        smpte: SMPTE divisionか
      ]
    }
    """

    def __init__(self, header: _Header, tracks: list[_Track]) -> None:
        """
        {
          責務: [__init__: 全trackのtempo eventから決定的なtempo mapを作る]
          処理: [
            1: SMPTE divisionなら一定のtick/秒を使う,
            2: tempoを(tick, track, seq)順に並べ、同一tickは最後を採用する,
            3: 区間ごとの累積分子(整数)を計算する
          ]
          引数: [header: SMF header, tracks: 解析済みtrack]
          戻り値: []
        }
        """
        division = header.division
        self.smpte = bool(division & 0x8000)
        self.changes: list[tuple[int, int]] = []
        if self.smpte:
            fps = 256 - (division >> 8)
            per_frame = division & 0xFF
            # 29 means 29.97 drop-frame: 30000/1001 frames per second.
            fps_num, fps_den = (30000, 1001) if fps == 29 else (fps, 1)
            self.ticks = [0]
            self.numerators = [0]
            self.rates = [RATE * fps_den]
            self.denominator = fps_num * per_frame
            return
        ordered = sorted(
            (tick, track.index, seq, tempo) for track in tracks for tick, seq, tempo in track.tempos
        )
        by_tick: dict[int, int] = {0: DEFAULT_TEMPO_US}
        for tick, _, _, tempo in ordered:
            by_tick[tick] = tempo
        self.changes = sorted(by_tick.items())
        self.ticks = []
        self.numerators = []
        self.rates = []
        numerator = 0
        previous_tick, previous_tempo = 0, DEFAULT_TEMPO_US
        for tick, tempo in self.changes:
            numerator += (tick - previous_tick) * previous_tempo * RATE
            self.ticks.append(tick)
            self.numerators.append(numerator)
            self.rates.append(tempo * RATE)
            previous_tick, previous_tempo = tick, tempo
        self.denominator = division * 1_000_000

    def sample(self, tick: int) -> int:
        """
        {
          責務: [sample: tickを最も近い整数sampleへ変換する(0.5は切上げ)]
          処理: [1: tickを含む区間を二分探索する, 2: 整数分子を足して一度だけ丸める]
          引数: [tick: 0以上の絶対tick]
          戻り値: [48 kHz sample index]
        }
        """
        index = bisect.bisect_right(self.ticks, tick) - 1
        numerator = self.numerators[index] + (tick - self.ticks[index]) * self.rates[index]
        return (2 * numerator + self.denominator) // (2 * self.denominator)


def _pair_notes(
    track: _Track, sustain: str, budget: list[int]
) -> tuple[list[tuple[int, int, int, int, int]], dict[str, int]]:
    """
    {
      責務: [_pair_notes: 1 trackのnote on/offを(channel, key)単位で組にする]
      処理: [
        1: tickごとに、既存noteのoffとCC64を先に適用する,
        2: 同じtickのnote onを適用し、同一keyの重複onは前のnoteを閉じる,
        3: 同tickのonより後に書かれたoffは0長として捨て、前のoffはorphanとする,
        4: 未終了noteをtrack終端で閉じる
      ]
      引数: [track: 解析済みtrack, sustain: ignore/extend, budget: 残りnote数]
      戻り値: [((channel, key, start_tick, end_tick, velocity)のlist), 集計]
      エラー: [midi_too_many_notes: note上限超過]
    }
    """
    stats = {
        "retriggered": 0,
        "orphan_note_off": 0,
        "zero_length_dropped": 0,
        "unterminated": 0,
        "sustain_extended": 0,
    }
    notes: list[tuple[int, int, int, int, int]] = []
    active: dict[tuple[int, int], tuple[int, int]] = {}
    held: dict[tuple[int, int], tuple[int, int]] = {}
    pedal = [False] * 16

    def close(key: tuple[int, int], begun: tuple[int, int], tick: int) -> None:
        if begun[0] >= tick:
            stats["zero_length_dropped"] += 1
            return
        budget[0] -= 1
        if budget[0] < 0:
            raise _fail("midi_too_many_notes", f"MIDI file exceeds {MAX_NOTES} notes")
        notes.append((key[0], key[1], begun[0], tick, begun[1]))

    events = sorted(track.events, key=lambda item: (item[0], item[1]))
    position = 0
    while position < len(events):
        tick = events[position][0]
        group_end = position
        while group_end < len(events) and events[group_end][0] == tick:
            group_end += 1
        group = events[position:group_end]
        late_offs: list[tuple[tuple[int, int], int]] = []
        started: dict[tuple[int, int], int] = {}
        for _, seq, kind, channel, first, _ in group:
            key = (channel, first)
            if kind == "off":
                if key in active and active[key][0] < tick:
                    begun = active.pop(key)
                    if sustain == "extend" and pedal[channel]:
                        held[key] = begun
                        stats["sustain_extended"] += 1
                    else:
                        close(key, begun, tick)
                else:
                    late_offs.append((key, seq))
            elif kind == "sustain":
                down = first >= 64
                if pedal[channel] and not down:
                    for held_key in sorted(k for k in held if k[0] == channel):
                        close(held_key, held.pop(held_key), tick)
                pedal[channel] = down
        for _, seq, kind, channel, first, second in group:
            if kind != "on":
                continue
            key = (channel, first)
            if key in active:
                stats["retriggered"] += 1
                close(key, active.pop(key), tick)
            if key in held:
                close(key, held.pop(key), tick)
            active[key] = (tick, second)
            started[key] = seq
        # Only an off written after the same-tick on cancels it; an earlier orphan off must
        # not discard a note that starts at this tick.
        for key, seq in late_offs:
            if key in active and active[key][0] == tick and seq > started[key]:
                active.pop(key)
                stats["zero_length_dropped"] += 1
            else:
                stats["orphan_note_off"] += 1
        position = group_end
    for key in sorted(held):
        close(key, held.pop(key), track.end_tick)
    for key in sorted(active):
        stats["unterminated"] += 1
        close(key, active.pop(key), track.end_tick)
    return notes, stats


def _decode(payload: bytes, encoding: str, *, strict: bool, what: str) -> str:
    """
    {
      責務: [_decode: MIDI text metaを指定文字コードで復号する]
      処理: [1: strictなら復号失敗をエラーにし、そうでなければ置換文字にする]
      引数: [payload: bytes, encoding: 文字コード, strict: 厳密復号か, what: 対象名]
      戻り値: [str]
      エラー: [midi_invalid_text_encoding: strictで復号できない]
    }
    """
    try:
        return payload.decode(encoding, errors="strict" if strict else "replace")
    except UnicodeDecodeError as error:
        raise _fail("midi_invalid_text_encoding", f"{what} is not valid {encoding} text") from error


def _select(
    tracks: list[_Track],
    paired: dict[tuple[int, int], int],
    options: dict[str, Any],
) -> tuple[int, int, str]:
    """
    {
      責務: [_select: 採点melodyとして使う(track, channel)を決定的に1つ選ぶ]
      処理: [
        1: noteを持つ(track, channel)を候補にし、既定でdrum channelを除く,
        2: track/channel指定で絞り込む,
        3: 1候補ならそれを、複数ならmelody名のtrackが唯一の候補を持つ場合だけ選ぶ
      ]
      引数: [tracks: 解析済みtrack, paired: (track, channel)ごとのnote数, options: 検証済み設定]
      戻り値: [(track, channel, 選択理由)]
      エラー: [
        midi_no_notes: 候補がない, midi_track_not_found: 指定に一致しない,
        midi_track_ambiguous: 自動選択できない
      ]
    }
    """
    candidates = sorted(
        unit
        for unit, count in paired.items()
        if count > 0 and (options["include_drum_channel"] or unit[1] != DRUM_CHANNEL)
    )
    if not candidates:
        raise _fail("midi_no_notes", "MIDI file has no melodic notes to import")
    explicit = options["track"] is not None or options["channel"] is not None
    if options["track"] is not None:
        candidates = [unit for unit in candidates if unit[0] == options["track"]]
    if options["channel"] is not None:
        candidates = [unit for unit in candidates if unit[1] == options["channel"]]
    if not candidates:
        raise _fail("midi_track_not_found", "No notes match the requested track/channel")
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], "explicit" if explicit else "single-candidate"
    named = []
    for unit in candidates:
        name = tracks[unit[0]].name
        text = _decode(name, options["text_encoding"], strict=False, what="name") if name else ""
        if any(word in text.casefold() for word in MELODY_KEYWORDS):
            named.append(unit)
    if len(named) == 1:
        return named[0][0], named[0][1], "melody-track-name"
    listed = ", ".join(f"track {track}/channel {channel}" for track, channel in candidates)
    raise _fail(
        "midi_track_ambiguous",
        f"Several tracks/channels contain notes ({listed}); specify track and/or channel",
    )


def _monophonic(
    notes: list[tuple[int, int, int]], overlap: str
) -> tuple[list[list[int]], dict[str, int]]:
    """
    {
      責務: [_monophonic: 採点用の単旋律へ正規化する]
      処理: [
        1: (start, -pitch, end)で並べる,
        2: 同じstart sampleの和音は最高音だけ残す,
        3: 前のnoteと重なる場合は前のnote終端を次の開始へ切り詰める(overlap=errorなら拒否)
      ]
      引数: [notes: (start_sample, end_sample, midi_note), overlap: truncate/error]
      戻り値: [[start, end, midi]のlist, 集計]
      エラー: [midi_overlap: overlap=errorで重なりがある]
    }
    """
    stats = {"chord_dropped": 0, "overlap_truncated": 0}
    result: list[list[int]] = []
    for start, end, pitch in sorted(notes, key=lambda item: (item[0], -item[2], item[1])):
        if result and start < result[-1][1]:
            if overlap == "error":
                raise _fail(
                    "midi_overlap", f"Notes overlap at sample {start}; melody must be monophonic"
                )
            if start == result[-1][0]:
                stats["chord_dropped"] += 1
                continue
            result[-1][1] = start
            stats["overlap_truncated"] += 1
        result.append([start, end, pitch])
    return result, stats


def import_smf(data: bytes, options: Mapping[str, Any] | None = None) -> ImportedScore:
    """
    {
      責務: [import_smf: SMF format 0/1を正規notes.jsonと取込evidenceへ変換する]
      処理: [
        1: optionsと最大サイズを検証する, 2: SMFを有界に解析する,
        3: tempo mapを作り、全trackのnoteを組にする, 4: melodyの(track, channel)を選ぶ,
        5: 48 kHz sampleへ変換し、offset/transposeを適用し、0長noteを捨てる,
        6: 単旋律化し、時間範囲を検証して文書を作る
      ]
      引数: [data: file bytes, options: MidiImportOptionsに従う設定]
      戻り値: [ImportedScore]
      エラー: [
        invalid_options, midi_too_large, midi_malformed, midi_unsupported_format,
        midi_too_many_tracks, midi_too_many_events, midi_too_many_notes, midi_no_notes,
        midi_track_not_found, midi_track_ambiguous, midi_overlap, midi_negative_time,
        midi_invalid_pitch, midi_duration_too_long, midi_exceeds_song_duration,
        midi_invalid_text_encoding
      ]
    }
    """
    if not isinstance(options, Mapping | None):
        raise _fail("invalid_options", "MIDI import options must be a mapping")
    settings = validate_adapter_options(
        MidiImportOptions, dict(options or {}), "Invalid MIDI import options"
    )
    if not isinstance(data, bytes | bytearray):
        raise _fail("midi_malformed", "MIDI input must be bytes")
    data = bytes(data)
    if len(data) > MAX_IMPORT_BYTES:
        raise _fail("midi_too_large", f"MIDI file exceeds {MAX_IMPORT_BYTES} bytes")
    header, tracks = _parse(data)
    tempo_map = _TempoMap(header, tracks)

    note_budget = [MAX_NOTES]
    paired_notes: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    pairing_stats: dict[str, int] = {}
    for track in tracks:
        notes, stats = _pair_notes(track, settings["sustain"], note_budget)
        for name, value in stats.items():
            pairing_stats[name] = pairing_stats.get(name, 0) + value
        for channel, key, start, end, _ in notes:
            paired_notes.setdefault((track.index, channel), []).append((start, end, key))
    track_index, channel, reason = _select(
        tracks, {unit: len(rows) for unit, rows in paired_notes.items()}, settings
    )

    offset = settings["offset_samples"]
    transpose = settings["transpose_semitones"]
    converted: list[tuple[int, int, int]] = []
    rounding_dropped = 0
    for start_tick, end_tick, key in paired_notes[(track_index, channel)]:
        start = tempo_map.sample(start_tick) + offset
        end = tempo_map.sample(end_tick) + offset
        if start == end:
            rounding_dropped += 1
            continue
        if start < 0:
            raise _fail("midi_negative_time", f"offset_samples moves a note before 0 ({start})")
        pitch = key + transpose
        if not 0 <= pitch <= 127:
            raise _fail("midi_invalid_pitch", f"Transposed MIDI note {pitch} is outside 0..127")
        converted.append((start, end, pitch))
    if not converted:
        raise _fail("midi_no_notes", "Selected track has no notes with positive duration")
    melody, mono_stats = _monophonic(converted, settings["overlap"])
    last_end = max(end for _, end, _ in melody)
    if last_end > MAX_DURATION_SAMPLES:
        raise _fail("midi_duration_too_long", f"Notes end at {last_end}, beyond 30 minutes")
    if settings["duration_samples"] is not None and last_end > settings["duration_samples"]:
        raise _fail(
            "midi_exceeds_song_duration",
            f"Notes end at {last_end}, beyond song duration {settings['duration_samples']}",
        )

    prefix = settings["note_id_prefix"]
    version = {"format_version": 1, "analysis_version": settings["analysis_version"]}
    notes_document = {
        **version,
        "notes": [
            {
                "id": f"{prefix}-{number:06d}",
                "start_sample": start,
                "end_sample": end,
                "midi_note": pitch,
                "pitch_confidence": None,
                "lyric_segment_id": None,
            }
            for number, (start, end, pitch) in enumerate(melody, start=1)
        ],
    }
    lyric_rows = sorted(
        (tick, track.index, seq, payload) for track in tracks for tick, seq, payload in track.lyrics
    )
    if len(lyric_rows) > MAX_LYRIC_EVENTS:
        raise _fail("midi_too_many_events", f"MIDI file exceeds {MAX_LYRIC_EVENTS} lyric events")
    encoding = settings["text_encoding"]
    evidence_document = {
        **version,
        "importer": {"id": IMPORTER_ID, "version": IMPORTER_VERSION, "policy": POLICY_VERSION},
        "sample_rate": RATE,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "smf_format": header.format,
        "track_count": header.track_count,
        "division": (
            {"type": "smpte", "raw": header.division}
            if tempo_map.smpte
            else {"type": "ticks_per_quarter", "value": header.division}
        ),
        "tempo_map": [
            {"tick": tick, "sample": tempo_map.sample(tick), "us_per_quarter": tempo}
            for tick, tempo in tempo_map.changes
        ],
        "tempo_events_outside_first_track": sum(
            len(track.tempos) for track in tracks if track.index > 0
        ),
        "time_signatures": [
            {
                "tick": tick,
                "sample": tempo_map.sample(tick),
                "numerator": numerator,
                "denominator": denominator,
            }
            for tick, _, numerator, denominator in sorted(
                (tick, track.index, numerator, denominator)
                for track in tracks
                for tick, numerator, denominator, _, _ in track.time_signatures
            )
        ],
        "selection": {
            "track": track_index,
            "channel": channel,
            "reason": reason,
            "track_name": (
                _decode(tracks[track_index].name, encoding, strict=False, what="name")
                if tracks[track_index].name
                else None
            ),
        },
        "options": settings,
        "counts": {
            **pairing_stats,
            "rounding_dropped": rounding_dropped,
            **mono_stats,
            "imported": len(melody),
            "tracks_missing_end_of_track": sum(not track.has_end_of_track for track in tracks),
        },
        "lyric_events": [
            {
                "track": index,
                "source_sample": tempo_map.sample(tick),
                "text": _decode(payload, encoding, strict=True, what="Lyric meta event"),
            }
            for tick, index, _, payload in lyric_rows
        ],
        "source_end_sample": tempo_map.sample(max(track.end_tick for track in tracks)),
        "confidence_calibrated": False,
    }
    return ImportedScore(IMPORTER_ID, notes_document, evidence_document)


class SmfImporter:
    """
    {
      責務: [SmfImporter: ScoreImporter契約でSMF取込を公開する]
      フィールド: [format_id: smf]
    }
    """

    format_id = "smf"

    def sniff(self, data: bytes) -> bool:
        """
        {
          責務: [sniff: MThd magicでSMFかを判定する]
          処理: [1: 先頭4 bytesを比較する]
          引数: [data: file bytes]
          戻り値: [SMFらしければTrue]
        }
        """
        return bytes(data[:4]) == b"MThd"

    def import_score(self, data: bytes, options: Mapping[str, Any]) -> ImportedScore:
        """
        {
          責務: [import_score: import_smfへ委譲する]
          処理: [1: import_smfを呼ぶ]
          引数: [data: file bytes, options: MidiImportOptions]
          戻り値: [ImportedScore]
        }
        """
        return import_smf(data, options)
