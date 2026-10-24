# コード設計レビュー: 命名・委譲・継承

> 対象: `main` `ec4fa4e8bb41d1801473c0649ea0a71d918810ac` のRust Core、Python Analysis、C# Client/GUIの実装を読み取り、代表的なAdapter/Backend/Detector・契約を比較した結果。実行結果と実機性能ではなく**コード構造**のレビューです。
>
> Review PR: この監査に伴う共通化変更と回帰テストは別ブランチで管理。2026-10-21時点の資料。

## 判定

| 確認項目 | 判定 | 根拠 |
|---|---|---|
| 類似する役割の命名 | **概ね良好** | Pythonの`*Adapter` / `*Backend` / `*Detector` / `*Optimizer`、C#の`ICoreApi` / `CoreApiClient` / `ICoreConnection` / `CoreConnection`、Rustの`AudioIo` / `AudioDeviceStreams`が役割を分けている |
| 共通動作の委譲 | **改善余地あり、一部修正** | `validate_options` のPydantic検証と`ServiceError`変換が各Adapterでコピーされていた。今回`validate_adapter_options`へ委譲 |
| 動作を持つアプリ固有基底クラスの継承 | **主要ロールでは確認されず** | Python ModelAdapterは`Protocol`による構造的契約で、各Adapterは継承せず実装。C#クライアントは`interface`を実装し、Rustはtrait/委譲 |
| フレームワーク由来の振る舞い継承 | **例外あり** | Python `HttpsRedirect(urllib.request.HTTPRedirectHandler)` が`redirect_request`をoverrideして`super()`を呼ぶ。設計原則の厳密適用なら改善対象。ただしHTTPS→HTTPダウングレード拒否のセキュリティを保つ必要がある |
| ベースフレームワーク型 | **例外として区別** | Pydantic `BaseModel`、Python `Exception`、C# `Exception`はフレームワーク契約に基づく継承であり、アプリの振る舞い再利用を目的とした基底クラス階層ではない |

## 名前の対応表

| 役割 | 共通契約 | 実装の命名例 | 方針 |
|---|---|---|---|
| 解析ユースケース | `ModelAdapter`（Python Protocol） | `LyricsAdapter`, `AlignmentAdapter`, `PitchAdapter`, `VocalEventAdapter`, `NotesAdapter`, `SongAdapter`, `StemsAdapter` | `<役割>Adapter`。複数形は`Role.LYRICS/STEMS/NOTES`と一致するため許容 |
| モデル推論エンジン | 現時点では役割別の暗黙契約 | `WhisperBackend`, `JapaneseCtcBackend`, `UmxHqBackend` | `<モデル名>Backend`。本当に同じメソッド契約のみProtocol化 |
| F0候補検出 | `PitchDetector`（Python Protocol） | `YinDetector`, `NormalizedAutocorrelationDetector` | `<アルゴリズム>Detector`、実装クラスに他検出器の振る舞いを継承しない |
| 譜面候補最適化 | `NoteOptimizer`（Python Protocol） | `TemporalDynamicProgrammingOptimizer` | `<手法>Optimizer` |
| GUI→Core通信 | `ICoreApi`（C# interface） | `CoreApiClient` | API呼び出しと接続プロセス管理を別責務にする |
| Core子プロセス | `ICoreConnection`（C# interface） | `CoreConnection` | CoreApiClientを**所有・委譲**し、HTTP実装を継承しない |
| Rust音声 | Rust trait / structと所有 | `AudioIo`, `AudioDeviceStreams` | 前者が音声バッファと入出力カウンタ、後者がデバイス/ストリームを担当 |

## 今回の修正

- `python/src/open_karaoke_analysis/adapters.py`に共通の`validate_adapter_options(schema, options, message)`を追加。
- `MockAdapter`・`LyricsAdapter`・`AlignmentAdapter`・`PitchAdapter`・`VocalEventAdapter`・`StemsAdapter`・`NotesAdapter`が検証、正規化、バリデーション例外変換を関数へ**委譲**。共通の振る舞いを含む基底Adapterは導入しない。
- `SongAdapter`はModel Schemaに加えて段階選択・配線のポリシーを検証するので、特殊条件の検証はロール内に残す。すべての機能固有検証まで一律共通化しない。
- 新しい`python/tests/test_adapter_design.py`で共通Error契約、厳格オプション検証、主要Adapterの具象クラス非継承を自動検証。
- モデル推論や外部I/Oの処理には変更を加えない。

## 続く改善候補

### P2: 重複I/Oの共通化

- `stem_backend.file_sha256`、`whisper_backend.sha256_file`、`alignment_backend.digest_file`、`vocal_event_features.sha256_file`にはSHA-256計算の共通部分が重複する。
- `notes._read_json`、`vocal_event_features.read_json_artifact`、`song._json`にも、JSON読込、サイズ制限、非有限数の拒否、エラー変換の同等処理がある。
- **ファイル読込/ハッシュ計算のみ関数へ委譲**し、ロール固有のサイズ上限、キャンセル、エラーコード、モデル重みの検証ポリシーは各呼び出し側に残す。既存テストで形式/安全性の差分を比較してから変更する。いきなり動作を統一してセキュリティ制約を失わない。

### P2: Backendライフサイクル契約の明示

- `WhisperBackend`, `JapaneseCtcBackend`, `UmxHqBackend`には`prepare()` / `identity()` / `close()`等の近似した操作があるが、引数・動作は役割ごとに異なる。
- 全Backendが同じであるかのような抽象基底クラスを作らず、**必要な役割単位でProtocol/Adapterを定義**する。例えば推論前ロード/close共通化が実際に必要になった段階で、契約とテストを先に定める。

### P2: HTTPS redirect hookの継承

- `alignment_backend.HttpsRedirect`は標準ライブラリの具体`HTTPRedirectHandler`を継承しており、厳密な「振る舞い継承は避ける」原則からは例外。
- 単に削除するとHTTPへのリダイレクトを事前拒否できなくなる可能性がある。ダウングレード/リダイレクト/SSRF相当の既存テストを維持し、HTTPダウンロード層を**ポリシーの委譲/明示的なリダイレクト追跡**に変更する案を検討する。安全性を優先し、テストなしでは置き換えない。

## レビュー時の原則

1. **同じ責務は同じ接尾辞・同じ契約**で呼ぶ。コードの役割名とデータ形式やIDは混同しない。
2. **共通の実装は関数、サービス、保持オブジェクトに委譲**。Adapterの振る舞い基底クラスを作らない。
3. Protocol / trait / interfaceは共通契約が必要な場合に限り導入。抽象化のための抽象化はしない。
4. **継承は原則として契約型/フレームワーク連携のみ**。外部ライブラリの既存フックで振る舞い継承が必要な場合は根拠を残し、安全な代替が確認できれば変更する。
5. インターフェースを揃えるために異なる入力前提、例外、エラーコード、キャンセル条件を消さない。
6. 共通化した処理の成功・失敗のテストを同時に用意し、CIで回帰検出する。

この文書は設計監査の記録です。P2項目は今回の変更で修正済みという意味ではありません。
