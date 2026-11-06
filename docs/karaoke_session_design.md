# 歌唱セッション統合設計

> 状態：**実装前の設計案**。再生用のCore APIや統合セッションが実装済みという意味ではない。
> 確認対象：2026-11-04時点の `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e`。Issue #106。
>
> 関連文書：[音声エンジン](audio_engine.md)、[音声クロック・遅延](audio_clock.md)、[採点](scoring.md)、[デスクトップクライアント](desktop_client.md)。
> PR #95 はCore APIの再接続・バージョン方針を変更する可能性があるが、現時点で未統合のため現在の実装として扱わない。

## この設計が必要な理由

現在のRust CoreにはCPAL/WASAPIストリーム、容量制限付き音声キュー、WAV供給処理、クロック・epoch変換、独立したMixerがある。
Pythonは曲データを生成でき、RustはそれをSongStoreへ取り込める。Avaloniaも曲データを読み、手動で指定した時刻の歌詞・目標音程を描画できる。

しかし、**選曲→伴奏再生→マイク入力→リアルタイム音高・採点→同期表示→結果保存**を一貫して管理する所有者が存在しない。

そこでRust Coreに `KaraokeSessionController` を設け、既存部品を**保持・委譲して組み合わせる**。UI・音声デバイス・モデルAdapterの具象クラスを継承して振る舞いを再利用しない。

## コンポーネントの責務と所有関係

| コンポーネント | 所有する責務 | 所有しない責務 |
|---|---|---|
| `KaraokeSessionController`（新設） | セッション状態遷移、`SongBundle`・音声asset検証、デバイス利用権、transport epoch、workerの生存期間、再生・一時停止・シーク・停止、診断とイベント公開 | callback内のPCM変換、Python推論、GUI描画 |
| `AudioDeviceStreams` / `AudioIo`（既存） | 実デバイス、固定容量キュー、callback入出力、時刻付き再生/録音フレーム、カウンター | 曲選択、採点、保存 |
| `ClockSession`（既存） | 48kHz整数サンプルの`ClockEpoch`、再生/録音別の基準点、遅延品質、時刻不連続の検出 | 遅延の物理測定、GUI独自の時計 |
| `WavFeeder`（既存） | 正規化済みWAVの非callback読込と時刻付きキュー投入 | 採点 |
| `Mixer`（既存） | 伴奏/監視マイクの音量、エコー・リバーブ、出力ブロック生成、DSP状態の事前確保 | 採点用rawマイク、通信・ファイルI/O |
| `RealtimePitchDetector`（新設予定のRust trait） | **エフェクト前の生マイク**から有界時間でF0・有声判定・時刻情報を生成 | デバイスcallback、オフラインPython実行 |
| `ScoringEngine`（新設予定） | 音程観測と目標ノートの対応、独立した評価指標、版付き採点結果 | モデル導入、GUI状態 |
| `KaraokeViewModel`（Avalonia側・既存） | Core状態を歌詞・音程バー・採点・回復操作の画面表示へ変換 | 音声クロックの正本、デバイス実装、採点計算 |
| Python Analysis Service（既存） | オフライン譜面/歌詞生成、必要な再解析、出典情報 | リアルタイムcallbackや歌唱中の採点 |

初期版では、**再生用デバイスの利用権を保持するセッションは1つだけ**とする。2曲目の開始要求は明示的に拒否または順番待ちとし、意図せず2曲を混ぜない。

### リアルタイム音声経路（KS-03/KS-04の設計）

現行の `WavFeeder` は `AudioIo` の出力キューへ直接PCMを投入する。そのまま別の書き手からMixer出力を追加すると伴奏を重複出力する。

**統合版では別の伴奏キューを作り、唯一の録音consumerで採点・監視へ分配し、Mixer出力だけを音声出力キューへ渡す。**

```text
検証済みWAV → 伴奏デコード → MusicQueue ────────┐
                                                    v
マイク → AudioIo録音 → CaptureDispatcher → MonitorQueue → PlaybackMixWorker → AudioIo再生 → WASAPI
                               │                         ↑
                               └→ ScoringQueue → F0/採点  Mixer
```

`MusicDecodeWorker`・`CaptureDispatcher`・`PlaybackMixWorker`の型と処理順は
[後半の#107実装詳細設計](#107-実装詳細設計オーディオ経路と状態所有)に定義する。
**この統合経路は未実装**。音声callbackでロック待ち、メモリ確保、ファイル読込、Python推論を行わない。

## セッションの状態遷移

| 状態 | 受け付ける操作・遷移 | 不変条件 |
|---|---|---|
| `preparing` | 検証成功→`ready`、失敗→`failed`、取消→`stopped` | デバイスを占有する前に保存曲・WAV実体・歌詞/譜面の版・モデル出典を検査 |
| `ready` | `play`、`seek`、`stop` | 採点はまだ確定しない。入出力デバイスと遅延校正状態を取得可能 |
| `playing` | `pause`、`seek`、`stop`、曲末イベント | 現在位置は**Coreの再生提示時刻**を正本とし、投入済みフレーム数から推測しない |
| `paused` | `resume`、`seek`、`stop` | 採点と再生位置を進めない。再開でepoch更新が必要な場合がある |
| `interrupted` | デバイス再選択・再校正→`ready`または`paused`、`stop` | 機器切断・クロック不連続・復旧不能な欠落を隠さず、無断で別デバイスへ切替えない |
| `finished` / `stopped` / `failed` | 状態の参照、資源の解放 | 終端状態。古いイベントや二重の採点確定を認めない |

更新イベントにはセッションの状態revisionと`instance_id`を付ける。`play`・`stop`等の冪等性を保証し、重複・順序違いのコマンドでストリームや採点を二重作成しない。正常・異常いずれの終了でもworkerとキュー・デバイスハンドルを解放する。

**シーク時:** epochを増やし、古い再生/録音バッファを無効化してから再基準化する。同期が戻るまでは採点を抑止し、すでに採点した音符を二重加点しない。途中シークした歌唱は練習として扱うか、新しい採点区間を開始する。全曲比較可能な最終得点は、連続歌唱または別途定義した練習区間集計ポリシーを満たす場合だけ確定する。

**クラッシュ・再起動時:** Coreの`instance_id`は変わる。旧セッションとメモリ内Jobは復活させない。GUIは認証を再確立して保存曲の開き直しを案内し、旧歌唱Jobを勝手に再送しない。PR #95と互換性を照合する。

## Core APIの公開境界（提案）

**現在は未実装。** 設計上は、冪等性キー付きの単一コマンドendpointを推奨する。個別の`/play`・`/pause`などを並行実装するとAPI契約が曖昧になるため、初回実装で正規形を1つに決める。

要求形式・状態スナップショット・期待revision・旧epoch・購読上限・エラーは
[後半のワイヤ契約案](#core-apiのワイヤ契約案41との統合前)を参照する。
公開APIの最終版管理・移行方針は#41（PR #95）と統合して決定する。

## クロックと診断の規則

- **出力したフレーム数は実際に音が聞こえる時刻ではない。** ハードウェア実測値または明示的な推定遅延補正から提示時刻を求める。未測定は`clock_quality: unknown`。
- 生マイクのフレームには`epoch`・`stream_sample`・有効/無効・不連続を付与する。出力と録音で独立した基準点を使い、エコー遅延を採点へ流用しない。
- 古いepochやマイク欠落区間の観測は**採点から除外して件数・長さを記録**する。「歌わなかった」と誤判定しない。
- OSの時刻が得られても、それだけで音響遅延を校正したことにはならない。30分以上のドリフトと物理loopbackのp95は#40/#12/#19で検証する。
- GUIは検証済みCoreスナップショット間の表示だけを、単調で有界な補間により滑らかにしてよい。ただし表示補間を採点の正本にしない。異なるホストの単調時計を共通の基準点として比較しない。

## リアルタイム音程・採点の契約

以下は#14向けの**未実装設計**である。既存のオフライン`pitch.py`合議を、リアルタイムマイク音程検出が完成したと解釈しない。

1. 音程workerは48kHz、mono、時刻付きのrawマイクPCMを有界の遅延で消費する。入力品質検査で無音・飽和・ノイズ・フレーム欠落・低エネルギーを区別し、無声音に架空のF0を補わない。
2. 差し替え可能な`RealtimePitchDetector`は有限な`f0_hz | null`、`voiced`、診断用`confidence`と`voiced_probability`、観測区間`[start_sample,end_sample)`、`epoch`、アルゴリズム版、拒否理由を返す。これらのconfidenceは**校正済みの正解確率とは限らない**。
3. `ScoringEngine`は観測時刻を測定済み/推定済みの音響遅延を使って曲の48kHz整数タイムラインへ写像し、正規化済み目標ノートへ対応づける。音符境界に許すずれは明示し、無制限の遅延許容を設けない。
4. `pitch`・`timing`・`coverage`・`stability`の指標を分離し、無効・未校正区間のマスクを保存する。目標ノートがあり有声と判定した区間の符号付きcent誤差は`1200 * log2(f0_hz / target_hz)`で求め、ゼロ以下や非有限の音高を拒否する。
5. リアルタイム表示は**暫定結果**とし、有界な先読みで因果的に平滑化する。曲終了後にDTWを使う場合も歌唱タイミングの補正幅を版付きで制限する。無制限のDTWでタイミングの誤りを正解扱いしない。
6. 最終得点は`score_version`・`note_chart_version`・`detector_version`・`latency_calibration_id`・`unscored_reason_counts`・有効データのcoverageと信頼性を持つ。人間評価と照合していない段階で、科学的に検証された0〜100点を宣伝しない。

採点入力へエコー・リバーブ後の監視マイクを流さない。譜面品質が不確か、または機器遅延が不明な場合も練習として再生できるようにし、採点は暫定または利用不可と明示する。

## 検証項目と合格判定の範囲

| テスト領域 | 自動検証する契約 | そのテストでは証明できないこと |
|---|---|---|
| セッション状態機械 | 再生/一時停止/シーク/停止、重複要求、Core再起動、古いイベントの排除 | 物理デバイスの安定性 |
| クロック/epoch | 疑似ドリフト、負の補正値、キュー欠落、シーク/機器切替、異常時刻 | 実測の音響遅延 |
| Audio/Mixer | callbackでのメモリ確保・ロック回避、有界キュー、機器停止、rawマイクの分離 | 実歌唱精度 |
| 採点 | 人工的な±cent/時刻ずれ、無音、オクターブ誤り、欠落、評価値の単調性、結果の版 | 人間評価との一致 |
| GUI統合 | Core位置への追従、旧epochの排除、大量曲一覧、拡大率・キーボード・高DPIのsmoke | 実際の監視音声との同期 |
| 物理リリース検証 | Windows WASAPI実機、loopback中央値/p95、ドリフト、機材切替、長時間稼働 | 未検証機材での動作保証 |

**最初の縦断実装**は、既存の伴奏WAVと手元のMIDI譜面を使い、練習モードで歌詞/目標音程と時刻付きマイク音程を表示する。高精度の自動譜面生成を完成の前提にしない。その後に採点・全曲自動解析の登録経路を接続する。

## 依存関係と実装順序

**P1:** #40（クロック実測）、#41（Core API）、#14（リアルタイム音程・採点）、#36（曲取込）、#42（実歌唱評価）。
**P2:** #13（Mixer接続）、#16（GUI）、#43（資源競合の分離）、#39（Windows配布）。
#38の任意編集は後続に回し、#44の高度ASR合議研究は定量的な基線評価の後に着手する。

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

