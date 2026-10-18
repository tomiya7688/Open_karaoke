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
- JSON本文は64KiBまで（超過413）。未完了Job16件、保持履歴512件（受付超過429）。
- 曲登録のコピー・検査上限と4枠のI/O budgetは [SongStore import](song_import.md) に定義する。
- Jobはqueued→running→completed / failed / cancelled。登録確定開始後のcancel要求は受理しない。
- Core終了時は新規Job受付を503で停止し、未確定の登録へキャンセルを要求する。
- エラーは `{"error":"message"}`、レスポンスはCache-Control: no-store。秘密値をエラーへ含めない。

Job履歴はメモリ内のみ。破棄済み・再起動前Jobは404で、SSEのEvent-IDリプレイも未実装。
一方、登録済み曲はディスクにあり、新Coreから `/songs` で読める。
解析成果物、Job履歴、登録済みSongStoreはそれぞれ別の寿命を持つ。
同じtokenを使うクライアントは同じ信頼セッション。クライアント別ACL、接続/SSE総数制限、
別OSユーザー間の分離、悪意ある同一権限プロセスからの隔離は後続の#41/#2。

## 実装

`main.rs` / `session.rs` が起動・認証、`lib.rs` がJob、`analysis_service.rs` がworker監視、
`song_api.rs` / `song_library/` が曲登録と一覧・取得。
低水準 `app()` はin-processテストにも使うが、これを直接listenして認証を省略しない。
製品は必ず `CoreSession::router` を経由する。
