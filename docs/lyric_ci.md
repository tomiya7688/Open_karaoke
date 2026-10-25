# 合成歌詞CERのCI回帰ゲート（#18.4 / P1）

[Lyric CER regression](../.github/workflows/lyric-regression.yml) は固定の公開合成文字列を評価し、保存済みbaselineからのCER増加を検出する。ASR推論や実歌唱の品質を測るものではない。Job API runnerは後続とする。P0は予約したままにする。

## 入力と判定

`python/tests/fixtures/lyric_ci_pairs.json` は日本語、Unicode正規化、反復の3ケース。`lyric_ci_baseline.json` は独立して保存したversion 1の集計であり、CIでは再生成しない。期待値はCER/WERとも0、参照17文字・5語。許容CER増分は0、候補の絶対CER上限は0.1。参照hashや評価条件が異なれば比較不能として失敗する。

候補の絶対ゲートが失敗してもサマリーが保存されれば比較を行い、失敗の集計を残す。比較不能・不正入力では回帰JSONは生成されない。評価開始前の依存導入失敗等では集計がないことがある。失敗時もartifact uploadを試み、結果を成功扱いにはしない。

## 保存するartifact

PR、対象pathのmain push、手動実行で `lyric-cer-synthetic-metrics` を14日保存する。候補 `candidate.json` と判定 `regression.json` のみを明示列挙してuploadする。入力、詳細レポート、音源、個別ID、参照・予測の本文はupload対象にしない。非公開実データをこのworkflowへ持ち込まない。

baseline更新は固定入力・参照hash・正規化・モデルメタデータ・閾値と差分理由をPRで確認して行う。CI成功のための自動上書きや閾値緩和はしない。fixture整形でも生バイトhashが変わるためbaselineを明示的に点検する。

## 整形とローカル確認

リポジトリ直下でPython開発依存を導入した環境とNode 22を使用する。`npm ci --ignore-scripts --no-audit --no-fund` で管理済みPrettier 3.6.2を導入する。Ruffは既存のPython dev依存を0.17.2へ固定し、同じ版をCIで使用する。

```text
npm run format:lyric-ci
npm run format:check:lyric-ci
python -m pytest python/tests/test_lyric_ci.py python/tests/test_lyric_regression.py -q
```

整形対象は今回のPythonテスト、workflow、合成JSON、npm設定、この説明のみ。担当外のdirty変更、生成artifact、vendorやnode_modulesを一括整形しない。既存の索引Markdownは従来の表記を保って行だけ追記する。CLIの詳細は [Testing](testing.md) を参照する。
