# Karaoke session integration design

> Status: **proposed design, not an implemented playback API**. Source review: `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` (2026-11-04). Issue #106.
>
> See [Audio Engine](audio_engine.md), [Clock](audio_clock.md), [Scoring](scoring.md), [Desktop Client](desktop_client.md). PR #95 may change API reconnection/versioning; do not assume its unmerged changes are present.

## Why this contract is needed

Today, Rust has CPAL/WASAPI streams, a bounded audio queue, WAV feeder, clock/epoch mapping, and an independent mixer. Python can generate a song bundle and Rust can import it. Avalonia can fetch that bundle and draw a **static position-selected** lyric/pitch view. However, no one component yet owns **start song → audible playback + raw mic → realtime pitch/scoring → UI positions → result**.

**The missing owner is a Rust `KaraokeSessionController`.** It should coordinate existing parts via *composition*, not by making UI, audio device, or model Adapter classes inherit behavior from each other.

## Responsibility and ownership

| Module / object | Owns | Must not own |
|---|---|---|
| `KaraokeSessionController` (new Rust service) | Session state machine, `SongBundle`/asset validation, device lease, current transport epoch, worker lifetimes, start/pause/seek/stop, diagnostics and event publication | Physical callback sample conversions; Python inference; GUI rendering |
| `AudioDeviceStreams` + `AudioIo` (existing Rust) | Hardware stream, bounded queues, callback I/O, tagged playback/capture frames and counters | Song selection, grading, persistence |
| `ClockSession` (existing Rust) | 48 kHz integer `ClockEpoch`, separate output/capture anchors, latency quality, discontinuity detection | Device measurement or an independent GUI clock |
| `WavFeeder` (existing Rust) | Read normalized accompaniment on a bounded worker and enqueue tagged frames | Score computation |
| `Mixer` (existing Rust) | Gain, echo/reverb, monitor mixing into an output block; preallocation of DSP state | Raw microphone score samples; network/filesystem I/O |
| `RealtimePitchDetector` (proposed Rust trait) | Analyze bounded **raw pre-effect microphone** frames and return timestamped F0/voicing evidence | Device callbacks or offline Python model execution |
| `ScoringEngine` (proposed Rust struct) | Assign pitch observations to target notes, calculate independent metrics, persist versioned results | Model selection/download, GUI state |
| Avalonia `KaraokeViewModel` (existing) | Display current/next lyric, pitch bars, scores and actionable errors from Core snapshots | Audio clock, device selection implementation, note grading |
| Python Analysis Service (existing) | Offline song generation, optional reanalysis, provenance | Live audio callback or runtime grading |

Only one controller holds the active output-device lease in v1. A second user-facing session must be rejected or queued explicitly; it must never silently mix two songs.

### Proposed real-time topology

```text
Verified SongStore assets ─> WavFeeder ─> tagged Music PCM ────────────┐
                                                                     v
physical microphone ─> AudioIo raw queue ─┬─> realtime pitch worker    Mixer ─> device output
                                          │          │                  ^
                                          │          v                  │
                                          └──────> monitor branch ──────┘
                                                (gain/echo/reverb)
                         Score notes ─> ScoringEngine <─ timestamped F0
                                                │
                  Core state + clock + score events (bounded / SSE)
                                                │
                                         Avalonia renderer
```

The exact route from raw microphone frames into the playback mixer must be explicitly designed for **one stream clock domain**. A callback must not wait for a mutex, Python model, HTTP response, dynamic allocation, or disk access. If input/output devices have different clocks, use bounded queues with drift-compensated resampling on a **noncallback** worker. On underrun, emit diagnostics and silence; don't duplicate frames as if continuous.

## Session state machine

| State | Allowed commands/events | Contract |
|---|---|---|
| `preparing` | validation succeeds → `ready`; failure → `failed`; cancel → `stopped` | Verify stored song, backing WAV, note/lyric versions and model-provenance references before owning devices |
| `ready` | `play`, `seek`, `stop` | No score recorded. Input/output devices and latency status visible |
| `playing` | `pause`, `seek`, `stop`, end-of-song | Output position comes from Core presentation timestamp, **not** number of frames queued |
| `paused` | `resume`, `seek`, `stop` | Samples/score don't advance while paused; resume may require fresh epoch |
| `interrupted` | device reselect/recalibration → `ready` or `paused`; `stop` | Device removed, clock discontinuity, unrecoverable stream overrun; no hidden auto-switch |
| `finished` / `stopped` / `failed` | read final state, release resources | Terminal for this session, no stale events or duplicate final score |

A session state revision and `instance_id` accompany updates. `play` and `stop` should be idempotent; repeated/reordered commands must not create duplicate streams or grades. Termination cleans workers, queue ownership, and stream handles on every error path.

**Seek policy:** Increment the clock epoch, invalidate buffered playback/capture, reanchor and suppress scoring until aligned. A seek makes the current run a **practice attempt** or starts a new scoring segment; do not double-count already scored notes. A final, comparable whole-song score requires uninterrupted coverage or an explicitly defined policy for practice-segment aggregation. UI must show this distinction.

**Crash/restart policy:** Core process restart changes `instance_id`. Old sessions and in-memory Job IDs are not resurrected. GUI must rebootstrap/reauthenticate and offer to re-open a saved song; never resubmit a singing attempt automatically. Coordinate with PR #95.

## Proposed Core API boundary (not existing routes)

Do not route GUI directly to `AudioIo` or to Python's private port.

```text
POST /karaoke/sessions             choose persisted song + device profile
GET  /karaoke/sessions/{id}        authoritative state + position
POST /karaoke/sessions/{id}/play
POST /karaoke/sessions/{id}/pause
POST /karaoke/sessions/{id}/seek   integer song_sample + expected_epoch
POST /karaoke/sessions/{id}/stop
GET  /karaoke/sessions/{id}/events  bounded SSE state/pitch/score
```

Endpoints above are **conceptual**; #41 controls the final Core API version and client migration. Every mutating message must verify the authenticated owner, current session revision/epoch (where relevant), and bounded payload size. SSE must have bounded consumers/backpressure; loss of UI subscription cannot stop the audio callback.

A proposed snapshot (illustrative **not current API**):

```json
{
  "session_id": "<uuid>",
  "core_instance_id": "<uuid>",
  "state": "playing",
  "revision": 3,
  "song_id": "<uuid>",
  "sample_rate": 48000,
  "epoch": 2,
  "presented_song_sample": 128000,
  "clock_quality": "estimated",
  "scoring_mode": "practice",
  "audio": {
    "output_underrun_frames": 0,
    "input_overrun_frames": 0
  }
}
```

All persistent song/note/lyric times remain **u64 sample indices** at 48 kHz; JSON times expressed as seconds are display-only. Clients discard events with old `core_instance_id`/`session_id`/`epoch` or lower revisions.

## Clock and observability rules

- **Output counter is not acoustic presentation time.** Compute song presentation from hardware measurements or explicitly marked estimated correction; if unknown, report `clock_quality: unknown`.
- Raw capture frames carry `epoch`, `stream_sample`, valid/invalid and discontinuity indicators. Clock mapping uses output/capture anchors separately; do not reuse echo latency for scoring.
- Score observations from stale epochs or input-frame gaps are **excluded and reported**, not treated as missed singing. Record the count and duration of excluded samples.
- An OS-provided timestamp is not automatically a calibrated acoustic delay. 30+ minute drift and physical loopback p95 belong to #40/#12/#19.
- The UI may interpolate smoothly between **validated** Core snapshots with a bounded, locally monotonic display estimator, but the estimator is never the scoring truth. Do not compare GUI-host and Core-host monotonic timestamps as if they had the same origin.

## Real-time pitch and scoring contract

This is a **target** for #14; the offline `pitch.py` ensemble is not a realtime microphone implementation.

1. The realtime worker consumes timestamped raw mono 48 kHz PCM with bounded latency. A lightweight input-health stage detects silence, saturation, noise, gaps and low-energy frames; it must not silently invent an F0 on unvoiced inputs.
2. A replaceable Rust `RealtimePitchDetector` returns finite `f0_hz | null`, `voiced`, diagnostic `confidence` and `voiced_probability`, the observation window `[start_sample,end_sample)`, `epoch`, algorithm/version, and rejection reason. These values are **not automatically calibrated accuracy probabilities**.
3. `ScoringEngine` maps the observation's measured/estimated **acoustic** time into the canonical song timeline, then matches the normalized target notes. At note boundaries use a documented transition allowance, not unlimited lag forgiveness.
4. Preserve independent `pitch`, `timing`, `coverage`, `stability` metrics and a mask of invalid/uncalibrated intervals. For voiced observations with valid target note, define signed cent error as `1200 * log2(f0_hz / target_hz)`. Refuse nonfinite/zero pitch.
5. Realtime indicators are **provisional** and use causal (bounded-lookahead) smoothing. Optional post-song DTW may correct a bounded singer timing variation, but its warp budget and version must be fixed; unlimited DTW would make timing mistakes look correct.
6. Final score includes `score_version`, `note_chart_version`, `detector_version`, `latency_calibration_id`, `unscored_reason_counts`, and confidence/coverage of usable data. Without calibrated human ground truth, do not advertise a scientifically validated 0–100 result.

No score input flows from the effect-processed monitoring branch. If the chart cannot be trusted or hardware timing is unknown, keep a playable practice mode and mark grading as provisional or unavailable.

## Validation matrix / exit criteria

| Test | Automated expectation | Not proved by it |
|---|---|---|
| State machine contract | Play/pause/seek/stop, repeated requests, Core restart, stale event handling | Physical device stability |
| Clock/epoch | Simulated ±drift, negative offsets, queue gaps, seek/device change and invalid timestamps | Measured acoustic latency |
| Audio/Mixer | No allocation/lock in callback, bounded queues, device outage/overflow, raw mic not effected | Singing accuracy |
| Scoring | Artificial ±cents, offsets, silence, octave errors and device gaps; monotonic metrics; score version | Human judged quality |
| GUI integration | UI follows only Core authoritative position, stale epochs discarded, large song pages, zoom/keyboard/high DPI smoke | Real monitor/presentation synchronization |
| Physical release | Windows WASAPI devices, measured loopback median/p95, drift, mic/speaker switching, long playback | Claims on untested devices |

A useful **first vertical slice** is to render a manually supplied MIDI chart over a real WAV and display timestamped mic pitch in practice mode, without requiring high-quality automatic note generation. Then attach scoring and the full-song analysis import flow.

## Dependencies and sequencing

P1: #40 clock measurement, #41 API, #14 realtime pitch/scoring, #36 Core import, #42 real-singing set. P2: #13 mixer connection, #16 GUI, #43 resource isolation, #39 Windows packaging. Keep #38 optional editor as later work; keep #44 advanced ASR research after objective baselines.

## #107 実装詳細設計：オーディオ経路と状態所有

この節は**コードを書き始める際の設計入力**とする。既存のAPIはソース上の名前で、
未実装の型・操作には「新設」を付ける。実装で契約を変更する場合は**同じPRで本節を先に/並行して修正**し、
sourceとtestsとの差を残さない。

### 設計上の決定（PRレビュー対象）

| ID | 決定 | 理由 |
|---|---|---|
| KS-01 | `KaraokeSessionController`（新設）はCore内の**唯一の歌唱セッションowner**。GUIやPythonには状態遷移させない | 複数曲・device lease・seek・終了の競合を一箇所で防ぐ |
| KS-02 | 実行時オーディオは `AudioIo` / `AudioDeviceStreams` / `ClockSession` を**保持して委譲**。具体クラスの振る舞い継承はしない | 独立テストできる既存コンポーネントを維持 |
| KS-03 | マイクringは**1つのCaptureDispatcher（新設）が一度だけ読む**。採点枝とモニター枝へbounded fan-outする | `AudioIo::read_microphone_timestamped`は消費型であり、2人のconsumerが読むとPCMが分割・消失する |
| KS-04 | 新設 `PlaybackMixWorker` が伴奏・監視マイクを整列して `Mixer::process`し、その結果だけを `AudioIo::enqueue_stereo_at`へ送る | 現行 `WavFeeder` は`AudioIo`へ**直接送る**。そのままではMixerを挟めない |
| KS-05 | `ClockSession`の更新はControllerの非callback側に閉じ、output/captureの対応はepoch付きsnapshotとして配布 | 競合による旧フレームの混入を避ける |
| KS-06 | **練習（Practice）**は再生可能性を優先、**全曲採点（WholeSong）**は連続区間・校正品質を満たしたときだけ確定 | seekや音声欠落を誤って高得点/減点へ反映しない |

### 既存クラスをどの順番でつなぐか

現状の `WavFeeder::start_at(AudioIo, epoch, song_sample, path)` は
`AudioIo`のoutput ringへPCMを直接入れる。ここにもう一つMixerからのPCMを入れると
伴奏とミックス済みPCMが二重に再生されるため**禁止**する。

**新設する最小限の部品**:

- `WavFrameReader`（新設、Data/非callback）: 既存`audio_wav.rs`のヘッダー検証・シーク・フレーム読込を一つの共通実装として切り出す。受け取るのは検証済みファイルハンドルと開始sample。非有限PCMは拒否する。
- `MusicDecodeWorker`（新設、Process）: `WavFrameReader`へ委譲し、`MusicFrame { epoch, song_sample, stereo }`を専用のbounded `MusicQueue`へ送る。再生中のreadは非callbackスレッドで実行する。旧`WavFeeder`は同じReaderへ委譲して従来の単体再生互換を保持する。
- `CaptureDispatcher`（新設、Process）: 唯一のconsumerとして`AudioIo::read_microphone_timestamped`を呼び、`RawCaptureFrame {epoch, stream_sample, mono}`を**別々の**採点キューとモニターキューへ発行する。どちらの遅さももう一方をブロックしない。
- `PlaybackMixWorker`（新設、Process）: 音楽フレームと「出力時刻に対応づけた」マイクフレームを読み、事前確保したblockで`Mixer::process`へ委譲し、`AudioIo::enqueue_stereo_at`へ**1経路のみ**書く。
- `ScoringWorker`（#14で新設）: 採点キューのraw PCMのみ利用する。`Mixer`処理後の信号・リバーブ・伴奏を受け取らない。

```text
SongLibrary ── verified accompaniment ── WavFrameReader
                                          │
                                  MusicDecodeWorker
                                          │
                                      MusicQueue ─────────────────┐
                                                                   v
Mic hardware ── AudioIo capture ring ── CaptureDispatcher ── MonitorQueue ── PlaybackMixWorker
                                           │                             │        │
                                           └── ScoringQueue ── F0/Score  │       Mixer
                                                                         │        │
                                                         ClockSnapshot ──┘        v
                                                                            AudioIo output ring
                                                                                 │
                                                                             WASAPI output
```

**制約**: `AudioIo`、`ClockSession`、`Mixer`を継承する新しい基底クラスを作らない。
委譲・保持関係にする。共通WAVデコードを2回実装しない。音声callback内でロック、
I/O、Python、メモリ確保、HTTP、動的なモデル読み込みをしない。
既存`AudioIo`のoutput_writer lock / microphone_reader lockは非callback側でのみ取得する。

**マイク監視と採点の時刻は別**: マイクモニター用の遅延補正を採点へ流用しない。
Device capture/outputが異なるclockで動く場合は、noncallback workerで有界ドリフト追従・
リサンプリングを行う。これは初期デバイスプロファイルで同一48 kHzと確認できても不要とは断定しない。
`CaptureDispatcher`は同じrawフレームの複製を作るが、両枝の**ドロップ数と理由**を個別に持つ。
採点枝が遅いときはその区間を評価対象外として計数し、無音で歌っていないと判定しない。
モニター枝が遅いときは古い監視フレームを落とし、エコーとループバックが蓄積しないようにする。

### Rustの型と公開操作（新設する契約の下書き）

以下は**コンパイル可能コードではなく設計上の署名**。既存実装の確定済みAPIではない。

```rust
struct KaraokeSessionController {
    active: Option<ActiveKaraokeSession>, // mutable state はこのownerだけ
    // verified SongLibrary、device provider、session/event storeは委譲で保持
}
struct ActiveKaraokeSession {
    id: Uuid,
    instance_id: Uuid,
    song_id: Uuid,
    state: KaraokeState,
    revision: u64,
    clock: ClockSession,
    audio: AudioIo,
    device: AudioDeviceStreams,
    music: Option<MusicDecodeWorker>,
    mixer: Option<PlaybackMixWorker>,
    capture: Option<CaptureDispatcher>,
    score: Option<ScoringWorker>,
    // device lease / cancellation / bounded event publisher
}
enum KaraokeState {
    Preparing, Ready, Playing, Paused, Interrupted, Finished, Stopped, Failed
}
enum KaraokeCommand {
    Play, Pause, Seek { song_sample: u64 }, Stop
}
struct CommandEnvelope {
    session_id: Uuid,
    expected_revision: u64,
    command_id: Uuid, // 再送時の二重処理を防ぐ
    command: KaraokeCommand,
}
```

- `ClockSession`と`AudioIo`の関係は**1セッション1対**。Controller以外にepochを増やす権限を与えない。
- `ActiveKaraokeSession`は状態とworkerの**所有権**を持つ。`AudioDeviceStreams::open`が返す物理Streamの終了は、このownerだけが指示する。GUIへraw handleを渡さない。
- `SongLibrary::load`は曲メタデータを検証するが、**毎回全音声をhash再検査するAPIではない**。再生直前に検証済みcanonical accompaniment assetを開く処理（新設`SongAssetLease`など）が必要。クライアントの絶対pathは受け取らない。ライブラリが所持する安全なRootから解決する。
- `Mixer::process(music, mic_raw, out)`は既存のブロック処理。非callback workerがblockの時刻と長さを整合し、結果を**1本の**output ringへ渡す。
- `WavFeeder::status().finished`は**queueへの送信が終わった状態**で、可聴再生の曲末とは異なる。`finished`判定は同じepochのoutput presentationが曲末へ到達したかを確認する。

### コマンド受付・順序・原子性

状態変更は非callbackの単一Controllerループで直列化する。HTTP handlerは認証・
要求サイズ・JSON検証後、bounded command channelへ要求を送り、workerを直接動かさない。

| 現状態 | Play | Pause | Seek | Stop |
|---|---|---|---|---|
| Preparing | 409 `not_ready` | 409 | 409 | 停止処理を開始 |
| Ready | Playing | 既存stateを返す（no-op） | Readyの位置を更新 | Stopped |
| Playing | 既存stateを返す（no-op） | Paused | 同一再生状態を再構成 | Stopped |
| Paused | Playing（新epochで再開） | 既存stateを返す（no-op） | Pausedの位置を更新 | Stopped |
| Interrupted | 409 `device_unavailable` | 409 | 409 | Stopped |
| Finished / Stopped / Failed | 409 `terminal` | 409 | 409 | 同じterminal snapshot（idempotent） |

- `expected_revision`が現状態と違えば**409 `stale_revision`**（何も変更しない）。
  同じ`command_id`と同じpayloadの再送は、保有期間内に記録した**同じ応答**を返す。
  同じ`command_id`で違うpayloadは409。`command_id`履歴はセッションあたり有界（例: 128件、最終値は実装時の測定で決定）とし、
  枠外の古い要求は安全側に拒否、別セッションを自動生成しない。
- 再生中のseekは「pause出力→旧Music/Monitoring/Score worker停止→epoch前進→
  `AudioIo::set_clock_epoch`→古いringをepochで無効→新しいsong位置へdecode起動→
  出力/入力anchor再確立→prebuffer→play」の順。中間失敗時は
  `Paused`または`Interrupted`とerrorを公開し、**旧音声・旧採点に戻さない**。
- Pause/Resumeもclock不連続として扱い、`ClockSession::observe_audio`の
  playing変化による**追加invalidating**を無視しない。controlが明示的にepochを進めた場合は、
  更新後の`AudioCounters`をbaselineとして観測し、snapshotの変化から同じ事象を二重検出しない設計を
  #40実装契約と一緒に確認する。二重に進めても構わない前提でテストせず、正しいepochの一致を検証する。
- device stream error・clock discontinuity・capture overflowの増加は同期された
  `Interrupted`または「区間を未採点」の方針で扱う（音声と採点が齟齬する状態で継続しない）。
- 再開後の練習/WholeSongを分ける。途中seekを含むセッションを連続全曲採点として確定させない。

### Core APIのワイヤ契約案（#41との統合前）

```text
POST /karaoke/sessions             { song_id, device_profile_id, scoring_mode }
GET  /karaoke/sessions/{id}        Snapshot（Revision、Epoch、Core instance IDを含む）
POST /karaoke/sessions/{id}/commands { command_id, expected_revision, command, song_sample? }
GET  /karaoke/sessions/{id}/events SSE（bounded events、単調増加event_seq）
```

旧節にある個別 `/play` / `/pause` / `/seek` / `/stop`は、
上記**単一command endpoint案との比較候補**。最初の実装ではどちらか1つの正規形を選んで
docsとC# clientとcontract testsを同時更新する。現時点で**いずれもルート未実装**。
以下の要素は個別endpointの場合も必要。

- `session_id`, `core_instance_id`, `state_revision`, `transport_epoch`,
  `event_seq`, `presented_song_sample: u64|null`, `clock_quality`,
  `scoring_mode`, `missing_audio_frames`, `input_overrun_frames`,
  `score_status`, `error_code`（任意）。
- `state_revision`は状態変更・seekなど**状態スナップショット**ごとに増やす。
  `event_seq`は再生・音高などのイベントごとに増やす。**音高イベントごとに状態revisionを増やさない**。
- 状態イベント・Pitch/scoreイベントは`instance_id/session_id/epoch`を必須とし、
  クライアントは旧世代や旧instanceのイベントを捨てる。SSEはboundedで、
  受信側が遅い場合はイベントを間引き、再接続は`GET snapshot`で復旧する。
  UI購読不能でも音声処理は続く。
- `401/403`: 認証/権限、`404`: セッション不在、`409`: 競合/古いrevision、
  `422`: 範囲外song sample、`503`: デバイス/再生準備失敗。
  エラー本文は既存のCore JSON error契約と整合させ、内部path・tokenを返さない。
- Core API v1/v2互換・再起動時のinstance照合は#41（PR #95）で決定し、
  この文書だけで互換性を確定しない。

### 検証に直結する設計不変条件

| テスト単位（新設予定） | 不変条件と最低シナリオ |
|---|---|
| `core/tests/karaoke_session.rs` | Ready→Playing→Paused→Resume→Stop、terminal再実行、同時セッション拒否 |
| `core/tests/karaoke_audio_graph.rs` | 伴奏とモニターを**1度だけ**混合、raw PCMからの採点分岐が不変、出力キューの二重供給禁止 |
| `core/tests/karaoke_epoch.rs` | Seek/device error/overrunで旧epochのplayback/mic/score/Pitchイベントを全廃棄 |
| `core/tests/karaoke_api.rs` | 401/404/409/422/503、冪等性、同じcommand_idの異なるpayload拒否、bounded SSE |
| `gui/OpenKaraoke.Client.Tests` | Core snapshotを位置の正本とする、旧`instance_id`/旧epochの通知を表示しない |
| `core/examples/wasapi_smoke.rs`拡張 | 物理デバイスの音とraw/mixedの時間関係。**CI合格の代替ではない手動実機試験** |

実装で変更した型名・引数・状態遷移がこの表と異なる場合は、
コードを先に作って閉じるのではなく、**設計案→実装差分→テスト**の順に同じPRで修正する。

