# Rust Core API

## 現在の公開エンドポイント

Rust Coreは既定で `http://127.0.0.1:32145` へbindします（`OPEN_KARAOKE_BIND` で変更可能。ただしloopback限定）。
v1契約では `GET /health` に `"api_version": 1` を追加し、GUI・CLI・Evaluatorは
`X-Open-Karaoke-Api-Version: 1` を付ける。既存クライアント移行のためヘッダー省略は現時点でv1とみなすが、
**明示的に不一致のバージョンは HTTP 426** として拒否する。JobとSSEイベントの既存フィールドは変えない。

| Method | Path | 用途 |
|---|---|---|
| GET | `/health` | Rust Core自身の生存確認 |
| GET | `/devices` | Windows WASAPIの48 kHz対応デバイス一覧（他OSは503） |
| GET | `/analysis/health` | 監視中のPython workerのhealth |
| GET | `/models` | workerに登録済みのAdapter一覧 |
| POST | `/jobs` | Job作成（HTTP 202） |
| GET | `/jobs/{id}` | Job状態・成果物 |
| POST | `/jobs/{id}/cancel` | Jobキャンセル要求 |
| GET | `/jobs/{id}/events` | JobスナップショットのSSE配信 |

このAPIが **GUI・Evaluatorが利用する公開境界** です。Python workerの `POST /analysis/{role}` は認証された内部APIであり、GUIから直接使う前提ではありません。Core上に `POST /analysis/lyrics` 等のルートが存在するわけではありません。

## v1の安全性・制限（Issue #41 部分実装）

- `/health` 以外のルートではブラウザの `Origin` ヘッダーを含む要求を **403** で拒否する。CORS許可ヘッダーは出さない。ネイティブGUI・CLIには `Origin` を付けない。
- JSONリクエスト本文の上限は **64 KiB**。超過時は **413**。大きな音声やモデルの実体をJob JSONに詰めず、管理対象artifactを参照する。
- 同時に未完了として登録できるJobは **16件**（超過は **429**）。Job記録は最大 **512件**、上限を超える場合は最も古い終了済みJobを破棄する。保管中のJobのSSE購読とsnapshot取得は従来通り、破棄されたJobは **404**。処理済みの音声成果物はこのポリシーで削除しない。
- SSEは再接続時、保持中Jobの最新snapshotを最初の `job` イベントで送る。Core再起動後のJob履歴復旧やEvent-IDリプレイは未実装であり、GUIは404/再接続に備える。
- エラーは引き続き `{"error":"message"}` を返す。version/Origin/件数・サイズ不正にも HTTP の対応するステータスを使う。

**残るセキュリティ上の制限:** `Origin` のない同一端末内の不正プロセスからはまだAPIを操作できる。
Core/APIの**セッション認証は未実装**であり、この変更だけでIssue #41の受け入れ完了とはしない。
CLI・Evaluator・GUIへの安全なトークン受け渡し、入力artifactの認可、job単位のアクセス制御、再起動時の状態同期を次段で実装する。
インターネットへ公開せず、GUI/CLI統合時にはsession authの完了をゲートとする。

## Jobの種類

`kind` に `analysis.<role>` を渡すとCoreがPython workerへ処理を転送します。現在のroleは `mock`, `stems`, `lyrics`, `alignment`, `pitch`, `events`, `notes`, `song` です。`song` は既定Adapterがなく未実装です。

モデルの登録状況、入力、出力は [Analysis Service](analysis_service.md) と [現在の実装状況](current_status.md) を参照してください。

### Python Jobの例（追加モデル不要のmock）

```powershell
$job = Invoke-RestMethod http://127.0.0.1:32145/jobs -Method Post -ContentType application/json -Body @'
{"kind":"analysis.mock","analysis":{"model_id":"mock-v1","options":{"steps":5,"delay_ms":10}}}
'@
Invoke-RestMethod "http://127.0.0.1:32145/jobs/$($job.id)"
```

ASRや分離処理を呼び出す際は、入力ファイルをCoreのartifact root配下へ置き、`analysis.input_artifact` にその相対パスを渡します。通常の絶対パスや外部URLは受け付けません。

### Framework smoke Job（内部テスト用）

```json
{"kind":"framework-smoke","steps":5,"fail_at_step":null}
```

`steps`、`fail_at_step` はsmokeテスト用です。解析処理のオプションではありません。

## Jobライフサイクル

状態は `queued` → `running` → `completed` / `failed` / `cancelled` です。Jobには `progress`、`stage`、時刻、`error`、`artifacts` が含まれます。

CoreとPythonのJob IDは別です。CoreがPython子プロセスを監視し、プロセス停止で実行中Jobが失敗した場合、完了したことにせずerrorとして公開します。SSEは購読時に現在のスナップショットを送ります。

## 保存と制限

Job履歴は現在 **メモリ内のみ** で、Core再起動後には復元しません。生成物は別のartifact directoryへ保存され、Job上では相対参照を返します。これは [Song Data Format](song_format.md) の `SongStore` と別物であり、Job完了だけで楽曲ライブラリへ自動登録されるわけではありません。

API契約とworkerセキュリティ・タイムアウトについては [Analysis Service](analysis_service.md) に、実装は `core/src/lib.rs`、`core/src/analysis_service.rs` にあります。
