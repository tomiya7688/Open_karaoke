# Licensing Policy

## Project license

Open_karaoke のプロジェクト本体は **Apache License 2.0** で提供する。

対象には、原則としてこのリポジトリで開発する以下を含む。

- Rust Core
- C# / Avalonia GUI
- Python Analysis Service
- 公開SDK / Plugin API
- プロジェクト独自の補助ツール

Apache-2.0 により、商用利用、店舗利用、改変、再配布を許可する。

## Scope of this policy

この文書の第三者ライセンス方針は、**Open_karaoke開発チームが公式に配布・推奨するバイナリ、モデル、Plugin、Model Registryの内容**に適用する。

Open_karaokeは、ユーザーが自分の責任で導入するMOD、Plugin、AIモデル、外部ツールについて、ライセンス種別だけを理由に技術的な利用を禁止しない。

したがって、ユーザーはGPL、AGPL、Non-Commercial、研究用途限定、独自ライセンス等のモデルやPluginをローカル環境で利用できる。ただし、その利用・複製・改変・再配布・店舗利用等が各ライセンスで許可されるかはユーザー自身が確認し、その条件に従う必要がある。

ユーザー導入コンポーネントはOpen_karaoke本体のApache-2.0へ自動的に再ライセンスされるものではない。

## Third-party dependencies

公式配布物へ含める第三者コンポーネントは、商用利用可能であることを最低条件とする。

確認対象はコードのライセンスだけではない。

- source code
- compiled binary
- AI model weights
- tokenizer / vocabulary
- runtime
- media codec
- bundled data
- redistribution terms
- attribution / NOTICE requirements

### 公式配布では原則として採用しないもの

- Non-Commercial (NC) 条項を含むもの
- 商用利用可否が不明なもの
- 学習済み重みの利用条件が不明なもの
- 公式配布物として再配布できないもの
- プロジェクト全体の配布条件と両立できないもの

GPL / AGPL 等は、プロジェクト全体へのライセンス波及や配布条件を個別評価し、公式配布物では原則回避する。

LGPL等は動的リンク、再リンク可能性、ソース提供等の条件を満たせる場合のみ採用を検討する。

これらは公式配布方針であり、ローカルMODとしての利用を禁止するものではない。

## AI models

### Official models

公式Model Registry / 公式配布物へ含めるモデルは、コードだけでなく学習済み重みの利用条件も確認済みでなければならない。

公式モデルのmanifestではライセンス情報を必須とする。

最低限:

```json
{
  "license": "MIT",
  "license_url": "https://example.invalid/license",
  "commercial_use": true,
  "redistribution": true
}
```

### User-supplied models / MODs

ユーザーがローカルに追加するモデルについては、Open_karaoke側でライセンスの許否判定を強制しない。

- NCモデル
- GPL / AGPL系モデル
- 研究用途ライセンス
- 独自ライセンス
- 再配布禁止モデル

なども、技術的に互換性があればローカル導入できる設計とする。

ただしOpen_karaokeは、それらの商用利用・店舗利用・再配布が許可されることを保証しない。

ライセンス情報が取得できる場合はUIへ表示し、公式検証済みモデルとユーザー導入モデルを明確に区別する。

ライセンス情報が不明なローカルモデルについても読み込み自体を一律禁止しないが、未検証であることを表示する。

## Third-party notices and inventory

Open_karaokeが公式に配布する第三者依存関係は `THIRD_PARTY_NOTICES.md` または生成されたSBOM / license inventoryへ記録する。

ユーザーが後から追加したMODやモデルは、公式配布物のSBOM対象外とする。

CI / Release Evaluationで少なくとも以下を確認する。

- dependency license inventory
- unknown license detection
- prohibited license detection for official distribution
- bundled AI weight license metadata
- FFmpeg等のビルド設定
- redistribution obligations

## FFmpeg and codecs

FFmpeg等を同梱する場合、実際のビルド構成によって適用ライセンスが変わるため、バイナリ単位で構成を記録する。

公式配布では、GPL / nonfree 構成を無意識に混入させない。

ユーザーが独自ビルドや外部ツールとして別構成を利用することまでは制限しない。

## Music, lyrics, and user content

Open_karaoke本体のApache-2.0ライセンスは、ユーザーが読み込む楽曲、録音物、歌詞、譜面等の権利を許諾するものではない。

店舗利用、複製、加工、保存、公衆送信、演奏等に必要な権利処理は、使用するコンテンツと利用形態に応じて別途必要になる。

そのため、製品説明では「特定の管理団体との契約だけで全利用形態が許諾される」とは保証しない。

## Release gate

商用利用・再配布条件を確認できない依存物は、1.0.0の**公式配布物**へ含めない。

このRelease gateはユーザーがローカル環境へ導入するMOD / Plugin / AI modelを対象としない。
