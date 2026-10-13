# Rust Core API

## 現在の公開エンドポイント

Rust Coreは既定で `http://127.0.0.1:32145` へbindします（`OPEN_KARAOKE_BIND` で変更可能。ただしloopback限定）。

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
