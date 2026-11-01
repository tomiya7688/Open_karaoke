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
明示版不一致・重複は426。省略の扱いを変える条件は [API版の互換性と移行方針](#api版の互換性と移行方針) を参照。

## 公開エンドポイント

| Method | Path | 用途 |
|---|---|---|
| GET | `/health` | Coreの生存確認。`api_version: 1`、秘密情報なし |
| GET | `/session` | 認証済みのinstance_idとAPI版 |
| GET | `/devices` | Windows WASAPIデバイス一覧（他OSは503） |
| GET | `/analysis/health` | 監視中Python workerのhealth |
| GET | `/models` | workerに登録済みのAdapter一覧 |
| POST | `/jobs` | 解析・framework・曲登録のJob作成（202） |
| GET | `/jobs/{id}` | Job状態・成果物・登録成功時の曲ID |
| POST | `/jobs/{id}/cancel` | Jobキャンセル要求 |
| GET | `/jobs/{id}/events` | 最新JobスナップショットのSSE配信 |
| GET | `/songs` | 登録済み曲のUUID cursor一覧。limit / after指定可能 |
| GET | `/songs/{id}` | 音声込み登録済み曲のSongBundle文書 |

GUI・Evaluatorはこの境界だけを利用する。Python workerの `POST /analysis/{role}` は
別の認証情報を持つ内部APIであり、GUIから直接接続しない。

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

## Core再起動を跨ぐ再接続契約

Jobの識別は `(instance_id, Job id)` の組。`instance_id` はCore起動ごとに新しく、readyと
`/session` で同じ値を返す。clientはreadyの値を保持し、`/session` と一致する間だけ同じsessionとみなす。

| 旧sessionのclientが観測するもの | 意味 | clientの対応 |
|---|---|---|
| 接続拒否・socket timeout | 旧Coreは停止済み | socket予算内に失敗する。自動で別Coreへ接続しない |
| 401 + `WWW-Authenticate: Bearer` | 新tokenで起動したCore（port再利用時） | session喪失。再bootstrapする |
| `/session` の `instance_id` が不一致 | 再起動後の別instance | session喪失。再bootstrapする |
| 旧Job idの `GET /jobs/{id}`・`/cancel`・`/events` が404 `{"error":"job not found"}` | Job履歴は引き継がれない | 同じinstanceなら未知Job、不一致なら再起動 |
| `song.import` の旧 `analysis_job_id` が404 | 同上 | 解析Jobを新sessionで再作成する |

- 起動元は再起動ごとに新しいtokenを生成する。規則に反してtokenを再利用しても、新Coreは別の
  `instance_id` を返し旧Jobは404になる（`session.rs` の契約テスト）。
- 再bootstrap = 旧接続の破棄 → 新しいtokenでCore起動 → ready → `/session` 照合。旧Jobを新sessionへ
  自動再送しない。登録済み曲はディスクにあり、新sessionの `/songs` で読める。
- GUI (`ShellViewModel`) は2秒ごとの `/session` heartbeatで401・instance不一致を検出して切断し、
  利用者の再接続を待つ。Python Evaluatorの `CoreJobClient` は `CoreJobError.code` で
  `unauthorized` / `session_mismatch`（再bootstrap要）と `job_not_found` / `transport` を区別し、
  Job作成前と404受信時に `/session` を再照合する。[Core Job Evaluator](core_job_evaluator.md) を参照。
- 検証: `core/src/session.rs` の `restart_contract_exposes_new_identity_and_rejects_old_jobs`、
  `python/tests/test_core_job_client.py` の再起動・401・404分類テスト、実Coreを使う
  `test_real_core_job_and_restart`（`OPEN_KARAOKE_TEST_CORE` 指定時、CIのdesktop-integration）、
  GUIの `--native` 再起動テスト。

## API版の互換性と移行方針

版は認証付きAPI全体で一つの整数。ready・`/health`・`/session` の `api_version` と要求ヘッダー
`X-Open-Karaoke-Api-Version` が同じ値を指す。現在の製品Coreはv1だけを提供する。

### v1のまま許可する変更（後方互換）

- 応答への任意フィールド追加（例: 登録Jobの `imported_song`）。clientは未知フィールドを読み捨てる。
  リポジトリ内のGUI・Python clientはそうしている。
- 新しいendpoint、Jobの `kind`、Adapter（`/models` の項目）の追加。
- 省略時に従来動作となる任意の要求フィールドの追加。
- 新しい制限による429・413・400の追加（既存の正常要求を拒否しない範囲）。
- SSEのEvent-IDリプレイ追加（リプレイを使わないclientの動作を変えない場合）。

### v2が必要な変更（非互換）

- 既存フィールド・route・Job `kind` の削除、改名、型や意味の変更。
- Job `status` の値追加（clientは未知statusを契約違反として拒否するため）。
- 既存条件に対するHTTP status、`{"error":...}` 形式、エラー接頭辞（`invalid_artifact_ref:` 等）の変更。
- 任意だった要求フィールドの必須化、上限の縮小。
- SSEのevent形式（data＝`GET /jobs/{id}` と同じsnapshot）や終端後に閉じる規則の変更。
- 認証方式、bootstrap、readyの形式変更。

### endpoint別の移行規則

| 対象 | v1 | v2導入時 |
|---|---|---|
| `/health` | 認証不要、`api_version: 1` | `api_version` はCoreの最上位版。対応版の配列を任意フィールドとして追加する |
| ready | `api_version: 1` | `/health` と同じ。clientは共通の最上位版を選び、共通版がなければ起動失敗として閉じる |
| `/session` | 認証済みinstance_idと `api_version` | 要求で解決した版を返す。clientは選んだ版との一致を照合する |
| `/jobs` | 本文・snapshot形式はこの文書 | Jobはinstance内のみで永続しないため、版をまたぐ移行データはない。snapshotは読む要求の版で表現する |
| `/models` | `{"models":[...]}` | 項目追加は互換。項目の形を変える場合はv2 |
| SSE | data＝v1 snapshot、終端で閉じる | 接続要求の版のsnapshotを送る |

SongStoreの保存形式とSongBundleのschema版はAPI版と独立に管理する。

### 版ヘッダー省略の扱い

1. **現在（v1だけ対応）**: 省略はv1とみなす。明示の `1` 以外・重複は426。リポジトリ内のclient
   （GUI、Python `CoreJobClient`）は常に明示の `1` を送る。
2. **v2対応Coreの最初のリリースから**: 省略は引き続きv1だけを意味し、決してv2とみなさない。
   省略は非推奨とし、文書と変更履歴で告知する。
3. **v1を削除するリリース**: 省略も明示の `1` も426（`Unsupported Core API version`）にする。
   v1の削除は、v1とv2を両方受け付けるリリースを最低一つ出し、リポジトリ内のGUI・Evaluatorが
   v2へ移行した後に限る。同時に対応する版は最大二つ（N と N−1）。

GUIはCoreと同じリリースで配布するため、互換期間は主に外部Evaluator・CLIスクリプト向け。
未知の版を受けたclientはfail closedで、別Coreや別版への自動fallbackはしない。

## 実装

`main.rs` / `session.rs` が起動・認証、`lib.rs` がJob、`analysis_service.rs` がworker監視、
`song_api.rs` / `song_library/` が曲登録と一覧・取得。
低水準 `app()` はin-processテストにも使うが、これを直接listenして認証を省略しない。
製品は必ず `CoreSession::router` を経由する。
