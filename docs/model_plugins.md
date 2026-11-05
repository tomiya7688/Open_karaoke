# Model Plugin Design

## 目的

初期実装は固定モデルで開始するが、将来的には開発者でなくてもモデルを追加・切り替えできる構造にする。

## 現在の実装と今後の設計

現在は `python/src/open_karaoke_analysis/service.py` が信頼済みのPython Adapterを**静的に登録**し、`GET /models` は `id / version / role / capabilities` を返します。

`models/<id>/manifest.json` を置いたフォルダは、起動中のAnalysis Serviceが `GET /model-packages`（Core経由も同名）で**検出して一覧にします**（#17の第1段）。manifestは次の通りで、読むだけでパッケージ内のコードは実行・importしません。

- 対象はmodels直下の実フォルダ（最大256件）。manifestがない重みだけのフォルダ（`whisper/`、`umxhq/`など）は一覧に出しません。
- manifestは通常ファイル・64KiB以下・UTF-8・`manifest_version: 1`。`id` はフォルダ名と一致し、`[a-z0-9][a-z0-9._-]{0,63}`。
- `status`: `available`（backendが登録済みAdapterで、roleも一致）、`unsupported_backend`、`role_mismatch`、`duplicate`（IDが重複、または組み込みAdapterのIDと衝突）、`invalid`（読めない・schema違反・未対応のmanifest_version）。不正なパッケージがあっても他の一覧とサービスは動き続けます。
- `origin` は常に `user_supplied` です。公式検証済みとは区別し、ライセンスは表示用の情報で審査はしません。

### 選択の保存（#17の第2段）

roleごとに「どのモデルを使うか」を1件だけ保存できます。候補は、登録済みの組み込みAdapterと、`status: available` のパッケージです。保存はデータだけで、パッケージのコードは実行しません。

- 保存先はartifact root直下の `model_selection.json`（`{"version":1,"selection":{"<role>":"<id>"}}`）。16KiB以下、一時ファイルへ書いてから置き換えます。
- `GET /model-selection` は、今も選べる選択だけを返します。消えた・利用不可になったパッケージ、未知のrole、IDの書式に合わない値、壊れた・巨大な・シンボリックリンクのファイルは無視します（ファイルは書き換えません。パッケージが戻れば選択も戻ります）。
- `POST /model-selection` は `{"role": "<role>", "model_id": "<id>" | 省略}` で保存・解除します。今そのroleで選べないID（未知・利用不可・別role・パス風の文字列）は `model_not_selectable`（409）で拒否します。パスとして解釈することはなく、IDは既知の候補との一致でのみ受け付けます。
- GUIの「モデルの選択」カードで、更新・role別の選択・保存・解除ができます。

### 選択の解析への反映（#17の第3段）

`POST /analysis/{role}` が `model_id` を指定していないとき、そのroleの保存済み選択でJobのAdapterを決めます。

- 優先順位: リクエストの `model_id`（最優先。保存済み選択は見ない）→ 保存済み選択 → 従来のroleの既定Adapter。
- 組み込みAdapterの選択はそのAdapter、パッケージの選択はそのmanifestの `backend`（登録済みAdapter）で動きます。データを読むだけで、パッケージ内のコードは実行・importしません。
- 保存済み選択が使えない（パッケージ削除・`available`でなくなった・role不一致・models/が読めない）ときはエラーにせず既定Adapterで動かし、`model_source: "default_fallback"` と短い理由で報告します。保存ファイルは書き換えません。
- Jobのスナップショットに `model_id`（実際に動かしたAdapter。従来どおり）、`model_source`（`request` / `selection` / `default` / `default_fallback`）、`selected_model_id`（保存済み選択のID。パッケージIDの場合あり）、`selection_fallback_reason` を追加しました。追加のみで、既存フィールドは変わりません。
- 制限: パッケージの選択は「そのbackend Adapterを使う」だけで、パッケージのフォルダや重みのパスはAdapterへ渡しません（重みの差し替えは未実装）。`song` Job内部の各段（stems・lyrics等）にも、受付時に `options.models`（最優先）→ 保存済み選択 → 既定の順で確定したプランを適用します（#109.1）。確定後に選択ファイルやpackageが変わっても進行中Jobは変わらず、各段の `selection`（source・保存ID・fallback理由）と `model_plan_resolved_at` を完了artifactのprovenanceへ記録します（`analysis_fingerprint` は変えません）。Core経由のJob応答への転送（`model_source`等と段別`model_plan`、worker値は長さ・形を検証し違反は`analysis_protocol`で失敗）は実装済みです（#109.2）。比較評価は`Job::require_exact_model_plan`でdefault_fallbackとprovenance欠落を拒否します（fail closed）。GUI表示、weight digestの記録は未実装です。

enable/disable、ZIPインポート、公式Model Registryはまだありません。manifestだけでは新しいPythonコードを追加できず、`backend` は既にインストール済みのAdapterを指します。下記の例は公式配布向けの設計例です。
追加のPython Adapterは任意コードを実行する**信頼済み拡張**です。ローカルモデルのライセンスチェックを省く方針は、未知の実行コードを安全に隔離できるという意味ではありません。

現在の状態は [現在の実装状況](current_status.md)、解析側の登録境界は [Analysis Service](analysis_service.md) に記載します。

## モデルロール

初期想定:

- asr
- stem_separator
- aligner
- pitch_detector
- note_transcriber
- validator
- optimizer

各ロールは共通インターフェースを持つ。

## 基本方針

アプリ本体はモデル固有のPythonコードや推論APIを直接知らない。

AI Analysis Serviceが各モデルをロードし、共通REST APIへ正規化する。

```text
App
 |
REST API
 |
Analysis Service
 |
Model Adapter
 +-- Whisper
 +-- Stem Separator
 +-- WhisperX
 +-- Future Model
 +-- Internal Model
```

## Official model manifest（将来のパッケージ形式）

公式配布・公式Model Registryへ登録するモデルはmanifestを持ち、ライセンス情報を必須項目とする。

初期例:

```json
{
  "manifest_version": 1,
  "id": "whisper-large-v3",
  "name": "Whisper Large V3",
  "role": "lyrics",
  "backend": "whisper-large-v3",
  "version": "1",
  "languages": ["ja", "en", "multi"],
  "license": "MIT",
  "license_url": "https://github.com/openai/whisper/blob/main/LICENSE",
  "commercial_use": true,
  "redistribution": true,
  "capabilities": {
    "timestamps": true,
    "word_timestamps": true,
    "confidence": true
  }
}
```

公式配布・公式Model Registryへ登録するモデルは、コードだけでなく学習済み重みの利用条件も確認済みであることを要求する。

以下は公式配布対象外とする。

- commercial_use != true
- redistribution条件を満たせない
- license不明
- モデル重みのライセンス不明
- Non-Commercial条項を含む

## User-supplied models / MODs

上記の制限は、Open_karaoke開発チームが公式に配布・推奨するモデルにのみ適用する。

ユーザーが追加するローカルモデルは公式ライセンス審査の対象外とし、ライセンス種別だけを理由にローダー側で一律拒否しない。

ライセンス情報が取得できる場合はUIへ表示し、公式検証済みモデルとユーザー導入モデルを区別する。

ライセンス情報がない、または公式審査を通過していないモデルは**将来のUI**上で `Unverified / User supplied` と表示できるようにする。現時点ではロード用UIがない。

## 将来の追加方法

候補:

1. 所定のmodelsフォルダへモデルパッケージを配置
2. GUIからZIP / packageをImport
3. 信頼できるModel RegistryからInstall

初期実装では 1 を最も単純な方式として想定し、2・3は後から追加する。

## 内製モデル

将来的に日本語歌唱向け内製モデルを合議メンバーとして追加できるようにする。

初期段階ではWhisper置換を目的とせず、Whisperと異なる誤り傾向を持つ補助ASR / CTCモデル等を想定する。
