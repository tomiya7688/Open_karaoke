# Rust Core API

## 起動と認証

Core実行ファイルはloopback専用。既定は `127.0.0.1:32145`、起動元は
`OPEN_KARAOKE_BIND=127.0.0.1:0` でOS割り当てポートを選べる。

実行ファイルのHTTP入口はセッション認証付き。起動元が暗号学的乱数32バイトを生成し、
64文字の小文字hexと改行をCoreのリダイレクトしたstdinへ渡す。
認証情報を引数・環境変数・URL・readyファイルへ置かない。
stdinを保持する起動元がセッションを所有し、閉じるとCoreとPython workerを停止する。
対話端末からの直接起動、不完全・不正なbootstrapは拒否する。
詳しくは [Desktop session](desktop_session.md) を参照。

`GET /health` だけは認証不要。それ以外は `Authorization: Bearer <token>` が必須。
`X-Open-Karaoke-Api-Version: 1` を付ける。版ヘッダー省略は移行用にv1として扱うが、
明示版不一致・重複は426。

## 公開エンドポイント

| Method | Path | 用途 |
|---|---|---|
| GET | `/health` | Coreの生存確認。`api_version: 1`、秘密情報なし |
| GET | `/session` | 認証済みのinstance_idとAPI版 |
| GET | `/mixer` | モニタ音量・エコー・リバーブ・bypass・マイクモニタの現在設定（#13） |
| PUT | `/mixer` | 8項目すべてを指定して置換。範囲外の有限値は丸め、適用後の値を返す。必須項目の欠落・未知項目は422 |
| GET | `/devices` | Windows WASAPIデバイス一覧（他OSは503） |
| GET | `/analysis/health` | 監視中Python workerのhealth |
| GET | `/models` | workerに登録済みのAdapter一覧 |
| GET | `/model-packages` | `models/<id>/manifest.json` のモデルパッケージ検出結果と利用可否（#17） |
| GET | `/model-selection` | roleごとに保存したモデル選択（今も選べるものだけ。#17） |
| POST | `/model-selection` | `{"role","model_id"}` で選択を保存。`model_id` 省略で解除。選べないIDは409、形式不正は422 |
| POST | `/jobs` | 解析・framework・曲登録のJob作成（202） |
| GET | `/jobs/{id}` | Job状態・成果物・登録成功時の曲ID |
| POST | `/jobs/{id}/cancel` | Jobキャンセル要求 |
| GET | `/jobs/{id}/events` | 最新JobスナップショットのSSE配信 |
| GET | `/karaoke-sessions/current` | 現行の歌唱セッションのsnapshot。無ければ404（#107） |
| GET | `/karaoke-sessions/{id}` | 歌唱セッションのsnapshot（未知は404） |
| POST | `/karaoke-sessions/{id}/commands` | play/pause/seek/stop。`command_id`で冪等、`expected_revision`で競合拒否（Stopは免除）。成功は`{disposition,snapshot}`、拒否は`{error,code}`（404/409/422） |
| GET | `/karaoke-sessions/{id}/events` | 状態イベントのSSE（同時8本まで、超過429）。`?after=N`/`Last-Event-ID`で再開、取りこぼしは`gap`+snapshot |
| GET | `/songs` | 登録済み曲のUUID cursor一覧。limit / after指定可能 |
| GET | `/songs/{id}` | 音声込み登録済み曲のSongBundle文書 |

GUI・Evaluatorはこの境界だけを利用する。Python workerの `POST /analysis/{role}` は
別の認証情報を持つ内部APIであり、GUIから直接接続しない。

## ミキサー設定（`/mixer`）

本文は `music_gain` / `mic_gain`(0..2)、`echo_level` / `reverb_level`(0..1)、
`echo_delay_ms`(1..1000)、`echo_feedback`(0..0.9)、`effects_bypass`、`mic_monitor`（既定false）。
JSONに表せない非有限値は拒否し、範囲外の有限値は範囲へ丸める。応答は実際に適用した値。
値は共有の `MixerControl` に書かれ、音声callbackは次の処理で反映する（API側は音声状態を触らない）。
Core単体ではデバイスを開かないため、デバイスを開く側が `AppState::mixer_control()` を
`AudioDeviceStreams::open_with_mixer_control` へ渡して接続する。物理遅延は未測定。

## 歌唱セッション（`/karaoke-sessions`、#107 107.2）

Coreが歌唱セッションの状態とコマンド順序の正本を持つ。クライアントは状態取得・transportコマンド・SSE購読だけを行い、
セッションの作成・準備完了・提示位置・採点区間・中断の報告はできない（将来のControllerがCore内部で行う）。
コマンド本文は `core_instance_id`（`GET /session`のinstance_id）・`command_id`（再送キー）・`expected_revision`・
`command`（`{"type":"play"|"pause"|"stop"}` / `{"type":"seek","song_sample":48kHz整数}`）。
再起動後の新しいCoreは旧セッションを知らず、旧instance宛は409 `stale_instance`。
位置は出力で提示済みの値のみ（`position_source`）。SSEはlive容量64を超えて遅れた購読者を閉じ、
再接続で`snapshot`/`gap`から復旧する。デバイスは開かない（実機の品質は未測定）。契約は
[設計](design/issue-107-karaoke-session.md)。

## Jobの種類

`kind: analysis.<role>` はCoreからPythonへ転送する。roleは
`mock`, `stems`, `lyrics`, `alignment`, `pitch`, `events`, `notes`, `song`。
`song-pipeline-v1` は正規化済みWAVから全解析を実行し、SongBundleとasset一覧を出力する。
元ファイルの正規化はまだJobへ未接続。[Full-song analysis](full_song_analysis.md) に要求例がある。

`kind: song.import` は完了済みのCore `analysis.song` Jobを検査し、音声込みで曲を登録する。
これはPythonへ転送せずCoreで実行する。要求は `song_import.analysis_job_id` のみで、任意の
入出力パスやbundleをクライアントから受け取らない。[SongStore import](song_import.md) を参照。

```json
{"kind":"song.import","song_import":{"analysis_job_id":"11111111-1111-4111-8111-111111111111"}}
```

登録成功Jobは `imported_song: {"song_id":"UUID","reused":false}` を追加する。
このフィールドは他Jobで省略される。worker相対パス用の `artifacts` は登録Jobでは空。

analysis Jobは任意で `model_id`・`model_source`・`selected_model_id`・`selection_fallback_reason`・`model_plan`（songのみ、段別の出所）を返す。worker値は検証し、違反は`analysis_protocol`で失敗（公開しない）。詳細は[設計](design/issue-109-model-plan.md)。
SSEにも同じJob snapshotを送る。画面はimport Job完了前に曲を登録済みと表示しない。

### 通信確認用mock

起動元がメモリ内に保持したtokenを使う（値を履歴へ直接入力しない）:

```powershell
$headers = @{ Authorization = "Bearer $sessionToken"; 'X-Open-Karaoke-Api-Version' = '1' }
$job = Invoke-RestMethod "$coreUrl/jobs" -Headers $headers -Method Post -ContentType application/json -Body @'
{"kind":"analysis.mock","analysis":{"model_id":"mock-v1","options":{"steps":5,"delay_ms":10}}}
'@
Invoke-RestMethod "$coreUrl/jobs/$($job.id)" -Headers $headers
```

ASR/分離の入力はCore artifact root配下へ置き、`analysis.input_artifact` に相対パスを渡す。
通常の絶対パスや外部URLは受け付けない。内部framework smokeは
`{"kind":"framework-smoke","steps":5,"fail_at_step":null}`。
`steps` / `fail_at_step` は解析・曲登録のオプションではない。

## 制限・失敗・終了

- 認証欠落・不正・重複Authorizationは401。`WWW-Authenticate: Bearer` を返す。
- 認証対象ルートのOriginまたはSec-Fetch-Siteは403。CORSは許可しない。
- 認証済み要求には数値loopbackのHostが必要。不正・重複は400。
- `analysis.input_artifact` はJob受付前にCoreが構文を検査する。`/` 区切りの正規化済み相対パス
  （空・`.`・`..` セグメント不可）、`\`・`:`・制御文字不可、1024文字まで。違反は400で、
  `error` は `invalid_artifact_ref:` または `artifact_ref_too_long:` で始まる（パス本文は含めない）。
  存在・symlink・root外への解決はworkerが従来どおり検査し、Job失敗として返す。
  `options` 内の参照（`lyrics_artifact` など）はworkerのAdapter契約で検査する。
- 完了した解析Jobが公開するartifact参照は256件まで、各参照は上と同じ構文規則。
  workerの結果が違反した場合、Coreは参照を公開せずJobを `failed`
  （`analysis_protocol: too_many_artifacts:` / `invalid_artifact_ref:` / `artifact_ref_too_long:`）にする。
- JSON本文は64KiBまで（超過413）。未完了Job16件、保持履歴512件（受付超過429）。
- Coreが同時に処理するHTTP要求は全ルート合計64件まで（超過429）。応答生成後に枠を返す。
- `/jobs/{id}/events` の同時SSE接続は全Job合計32件まで（超過429）。切断または終端Jobイベント送信後に接続を閉じて枠を返す。
- 曲登録のコピー・検査上限と4枠のI/O budgetは [SongStore import](song_import.md) に定義する。
- Jobはqueued→running→completed / failed / cancelled。登録確定開始後のcancel要求は受理しない。
- Core終了時は新規Job受付を503で停止し、未確定の登録へキャンセルを要求する。
- エラーは `{"error":"message"}`、レスポンスはCache-Control: no-store。秘密値をエラーへ含めない。
- 製品Coreはframework由来の404・405・不正なURL/本文のエラーもJSONへ揃える。405の`Allow`ヘッダーは保持する。

Job履歴はメモリ内のみ。破棄済み・再起動前Jobは404で、SSEのEvent-IDリプレイも未実装。
SSEの受信が遅れて更新を取りこぼした場合は接続を閉じる。クライアントは再接続し、最新snapshotから状態を復元する。
一方、登録済み曲はディスクにあり、新Coreから `/songs` で読める。
解析成果物、Job履歴、登録済みSongStoreはそれぞれ別の寿命を持つ。
同じtokenを使うクライアントは同じ信頼セッション。クライアント別ACL、TCP接続総数制限、
別OSユーザー間の分離、悪意ある同一権限プロセスからの隔離は後続の#41/#2。

## 実装

`main.rs` / `session.rs` が起動・認証、`lib.rs` がJob、`analysis_service.rs` がworker監視、
`song_api.rs` / `song_library/` が曲登録と一覧・取得。
低水準 `app()` はin-processテストにも使うが、これを直接listenして認証を省略しない。
製品は必ず `CoreSession::router` を経由する。
