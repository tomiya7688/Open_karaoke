# 設計書

> 状態: 最新 / 最終確認日: YYYY-MM-DD / 正本: このディレクトリ（日本語）

## このページの使い方

- まず読む: [概要とシステム境界](architecture/overview.md)
- 機能を変更する: 下の「機能別設計」の表から該当行を選ぶ。
- 判断の理由を知る: [判断記録（ADR）](decisions/README.md)
- 用語を確認する: [用語集](architecture/glossary.md)
- 設計書の書き方と検査: [設計書ルール](contributing/design-docs.md)

## 正本と更新ルール

- 設計の正本はこのディレクトリ。他の場所には要約とリンクだけを置く。
- 設計を変えたら、同じ作業でこの目次と該当文書を更新し、最終確認日を直す。
- コードと設計が食い違う場合の記録先: （記入）

## 全体設計

| 文書 | 何が分かるか |
| --- | --- |
| [概要・目的・制約](architecture/overview.md) | 目的、対象範囲、制約、用語 |
| [システム境界・外部連携](architecture/context.md) | 利用者と外部システムとの境界 |
| [構成（コンテナ図）](architecture/containers.md) | 主要な実行単位と通信 |
| [実行時の流れ](architecture/runtime.md) | 主要なシナリオの処理順 |
| [データ・永続化](architecture/data.md) | データ構造、保存先、移行 |
| [配布・実行環境](architecture/deployment.md) | 動作環境、配布、設定 |
| [横断ルール・品質](architecture/quality.md) | 性能、安全性、ログ、エラー方針 |

## 機能別設計

| 機能 | 何が分かるか | 設計書 | 状態 | 最終確認日 |
| --- | --- | --- | --- | --- |
| （機能名） | 担当範囲、利用者の目的 | [機能設計](features/example.md) | 提案 / 承認 / 実装済み | YYYY-MM-DD |

## 判断記録

- [ADR一覧](decisions/README.md)

## 英語版（任意）

- 翻訳版を置く場合は `docs/en/` に置く。日本語と食い違う場合は日本語が優先。
