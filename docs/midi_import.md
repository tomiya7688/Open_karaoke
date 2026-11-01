# MIDI / External Score Import

Issue #37. 既存の譜面データ（Standard MIDI File）を採点用の正規 `notes.json`（Song Data v1）へ
変換する。外部形式は正本にせず、Importerで内部形式へ変換する（[Song Data Format](song_format.md)）。

## 構成

- `python/src/open_karaoke_analysis/score_import.py`: Importer API。`ScoreImporter` Protocol
  （`format_id`、`sniff(data)`、`import_score(data, options)`）、`ImportedScore`、`ImporterRegistry`。
  他のカラオケ形式は新しい`ScoreImporter`を`registry.register()`するだけで追加できる。
  `default_registry()`は公式同梱のSMF Importerだけを登録する。
- `python/src/open_karaoke_analysis/midi_import.py`: リポジトリ内で自己完結したSMF reader/変換。
  外部依存を追加していない（THIRD_PARTY_NOTICESの追加なし）。
- Tests: `python/tests/test_midi_import.py`（MIDI bytesをtest内で組み立てるhand-authored fixture）。

現時点ではライブラリAPIであり、Core Job / GUIからの取込導線と `lyrics.json` 生成は後続。

## 対応範囲と上限

- SMF format 0 / 1。format 2は `midi_unsupported_format`。
- time division: ticks per quarter note、およびSMPTE（24/25/29.97/30 fps）。
- 上限: file 16 MiB、track 256、保持event 500,000、note 100,000、lyric event 100,000、
  出力の最終終端は30分（48 kHz × 1800 s）。解析ループはすべて入力bytesを消費して進むため有界。
- 未知のchunkは読み飛ばす。MTrk数はheaderと一致しなければならない。
- End of Trackは長さ0でなければならず、その後に同じMTrk内のbytes（padding含む）があれば`midi_malformed`。

## 時間変換（tempo map）

- 内部時刻は48 kHzの整数sample index。浮動小数は使わない。
- tick `t` の時刻は区間ごとの `Δtick × us_per_quarter × 48000` を整数で累積した分子を、
  `ppq × 1,000,000` で一度だけ割り、**0.5は切り上げ**で丸める。累積誤差は生じない。
- SMPTEは `tick × 48000 × fps_den / (fps_num × ticks_per_frame)`（29は30000/1001）。tempo eventは無視する。
- 既定tempoは500,000 us/quarter（120 BPM）。
- tempo eventは**どのtrackにあっても全体に適用**する（format 1では通常track 0）。track 0以外の
  tempo件数はevidenceの`tempo_events_outside_first_track`に記録する。同じtickに複数ある場合は
  `(tick, track番号, track内順序)` で最後のものを採用する。
- **変拍子（time signature）はsample位置に影響しない**。tickとtempoだけで時刻が決まる。
  time signatureはevidenceの`time_signatures`に記録するだけ（小節表示等の後続用途）。

## Note on/off

- note on velocity 0はnote offとして扱う。running status、sysex、metaを解釈する。
- 対応は(track, channel, key)単位。同じtick内では、(1) 既に鳴っているnoteへのoffとCC64、
  (2) note on、(3) 同じtickで始まったnoteへのoff、の順で処理する。これにより「次のnote onが
  前のnote offより先に書かれた」同tickの連続同音も2音として取り込む。同tickでonより後に書かれたoffは0長として捨て、onより前に書かれた対応のないoffはorphanとして無視する。
- 同じkeyのnote onが重複した場合は、前のnoteを新しいonの位置で閉じる（retrigger）。
- 対応するonのないoffは無視して`orphan_note_off`へ数える。終了しないnoteはtrack終端
  （End of Trackまたは最後のevent）で閉じ、`unterminated`へ数える。End of Track欠落は許容し件数を記録。
- sustain（CC64、値64以上でdown）: 既定`ignore`はnote offの位置を使う。`extend`はpedal中のoffを
  pedal解放、同keyの再打鍵、track終端のいずれか早い位置まで延長する。pedal状態は
  (track, channel)単位で、別trackのCC64は適用しない。

## Track選択

採点用melodyは単旋律の1系統（1 track × 1 channel）とする。

1. noteを持つ(track, channel)を候補にする。channel 10（0始まり9、GM drums）は既定で除外
   （`include_drum_channel`で含める）。
2. `track` / `channel` 指定があれば絞り込む（一致なしは`midi_track_not_found`）。
3. 候補が1つならそれを選ぶ。複数なら、track名に melody / vocal / voice / lead / メロ / ボーカル / 歌
   を含む候補が**ちょうど1つ**のときだけ選ぶ。それ以外は`midi_track_ambiguous`（候補一覧付き）で
   利用者に指定を求める。推測で選ばない。

## 重複・和音・重なり（単旋律化）

選んだnoteをsampleへ変換した後に適用する。

- sample丸めで0長になったnoteは捨てる（`rounding_dropped`）。
- 同じstart sampleの和音は**最高音だけ**を残す（`chord_dropped`）。
- 前のnoteの終了より前に次のnoteが始まる場合は、前のnoteの終了を次の開始へ切り詰める
  （`overlap_truncated`）。前のnoteは後続noteの後で再開しない（内包された短いnoteの後の残り区間は捨てる）。`overlap: "error"`なら`midi_overlap`で拒否する。

## 出力

- `notes_document`: `{format_version: 1, analysis_version, notes: [...]}`。各noteは
  `id`（`<note_id_prefix>-000001`形式）、`start_sample`、`end_sample`、`midi_note`、
  `pitch_confidence: null`（観測値ではないため）、`lyric_segment_id: null`。開始順・重なりなし。
  既存の正規notes検証（Python `song._canonical`、Rust `SongBundle::validate`の範囲・ID・音程規則）に適合する。
- `evidence_document`: importer id/version/policy、入力SHA-256、format、division、tempo map
  （tick・sample・us/quarter）、time signatures、選択track/channelと理由、各件数、
  lyric meta event（`source_sample`、`text`）。

## 音源との対応（拡張余地）

- `offset_samples`: 全noteへ加える相対オフセット（負値可。0未満になれば`midi_negative_time`）。
- `transpose_semitones`: キー差（0..127外は`midi_invalid_pitch`）。
- `duration_samples`: 曲の長さ。指定時は超過を`midi_exceeds_song_duration`で拒否する。
- lyric meta（FF 05）は`text_encoding`（utf-8 / cp932 / latin-1）で厳密に復号し、復号失敗は
  `midi_invalid_text_encoding`。歌詞segmentへの変換は後続。

## エラーcode

`invalid_options`、`midi_too_large`、`midi_malformed`、`midi_unsupported_format`、
`midi_too_many_tracks`、`midi_too_many_events`、`midi_too_many_notes`、`midi_no_notes`、
`midi_track_not_found`、`midi_track_ambiguous`、`midi_overlap`、`midi_negative_time`、
`midi_invalid_pitch`、`midi_duration_too_long`、`midi_exceeds_song_duration`、
`midi_invalid_text_encoding`。Registryは`score_import_unknown_format`、
`score_import_ambiguous_format`、`score_import_too_large`、`score_import_invalid_input`。

## 検証範囲

hand-authored fixtureによる変換規則の回帰のみ。市販・実在のカラオケMIDI corpusでの
track選択精度・歌詞文字コードの実態はUnverified。
