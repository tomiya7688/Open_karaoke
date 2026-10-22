# 実歌唱評価データの manifest（#42）

この契約は非公開の実歌唱評価データをローカルで検査する入口です。音源・歌詞・注釈・同意書をリポジトリへ追加しません。`datasets/` は Git 管理から除外します。データごとの利用許諾と歌唱者の同意は、担当者が `rights_reference` の原本で確認します。manifest 検査は権利の法的確認を代行しません。

## version 1

UTF-8 JSON の最上位は `version: 1` と `samples`（1〜10000件）です。各 sample は次の項目をすべて持ちます。

| 項目 | 内容 |
|---|---|
| `id` | dataset 内で一意の識別子 |
| `split` | `development` または `holdout` |
| `source_group` | 同じ曲・歌唱者・録音セッションなど、分割をまたがせない元データの単位 |
| `provenance` | 収録元と作成経緯を追跡できるローカルの参照 |
| `rights_reference` | 録音・実演・歌詞・注釈の利用範囲と同意を確認できるローカルの記録への参照 |
| `audio`, `annotation` | manifest のディレクトリからの相対 `path` とファイル全体の小文字16進 `sha256` |

`path` は `/` 区切りで、親ディレクトリ移動・絶対パス・manifest ディレクトリ外へのシンボリックリンクを拒否します。同じ `source_group` または同一音源 hash を development と holdout に分けることも拒否します。

```json
{
  "version": 1,
  "samples": [
    {
      "id": "local-take-001",
      "split": "development",
      "source_group": "local-session-001",
      "provenance": "private capture log reference",
      "rights_reference": "private consent and license record reference",
      "audio": {"path": "audio/take-001.wav", "sha256": "<64 lowercase hex characters>"},
      "annotation": {"path": "annotations/take-001.json", "sha256": "<64 lowercase hex characters>"}
    }
  ]
}
```

## 注釈の共通ヘッダー

`annotation.path` の JSON は 4 MiB 以下とし、`version: 1`、manifest と一致する `sample_id` と `audio_sha256`、`sample_rate: 48000`、正の `duration_samples`（最大4時間）、`kind`、空でない `payload` を持ちます。`kind` は `lyrics`、`alignment`、`pitch`、`events`、`notes`、`stems` のいずれかです。内部時刻は48 kHzの整数sample indexで表し、区間の終端は排他的です。
1つの sample は1種類の注釈を指します。同じ音声を複数種類で評価する場合は、同じ `source_group`・音源 hash と異なる `id` の sample を同じ split に置きます。表示される件数は音源数ではなく sample 件数です。

manifest と注釈の JSON は重複キーおよび `NaN`・`Infinity` を拒否します。

```json
{
  "version": 1,
  "sample_id": "local-take-001",
  "audio_sha256": "<audio file SHA-256 from manifest>",
  "sample_rate": 48000,
  "duration_samples": 96000,
  "kind": "lyrics",
  "payload": {"reference_segments": [{"id": "line-1", "text": "例", "start_sample": 0, "end_sample": 48000}]}
}
```

`kind: lyrics` の `payload` は `reference_segments` 配列を持ちます。各区間の `id` は一意、`text` は空白だけを除く1〜20000文字、`start_sample` と `end_sample` は整数で `0 <= start_sample < end_sample <= duration_samples` とします。配列は開始時刻順で、同時刻開始・区間の重なりは許容します。歌詞のない録音では空配列を使えます。歌詞参照の人手による正確さや文字正規化はこの検査の対象外です。

他の `kind` の `payload` 構造と区間境界、注釈精度は未検証です。`duration_samples` が音声の実フレーム数と一致するかも未検証です。これらは後続の種別別検査で扱います。

ローカルで `python -m open_karaoke_analysis.dataset_manifest datasets/manifest.json` を実行します。成功時は件数と hash 検査結果だけを表示します。公開 CI では private dataset を取得せず、検査結果や実データを公開 artifact にアップロードしません。実行済みモデルの実体確認、録音条件の固定、歌詞以外の指標との結合は #18 と #42 の後続単位です。

## 歌詞の集計評価（#42.4）

予測は非公開 JSON に保存します。`version: 1`、自己申告の `model.name`・`model.version`、`predictions` 配列を持ち、各行は `sample_id` と `text` です。選択した split に属する歌詞注釈の sample ID を過不足なく指定します。異なる split の予測を混ぜると拒否します。空の予測文字列は許容します。

```json
{
  "version": 1,
  "model": {"name": "local-asr", "version": "snapshot-1"},
  "predictions": [{"sample_id": "local-take-001", "text": "例"}]
}
```

`python -m open_karaoke_analysis.lyric_dataset_report datasets/manifest.json datasets/predictions.json --split development` は既存の歌詞評価器で CER/WER を集計します。1 split あたり歌詞 sample は1000件までです。参照文字列は開始時刻順の segment を空白で連結します。CER は空白を除去し、WER は空白区切りで数えるため、日本語の形態素単位の誤り率とは見なしません。長い歌唱は評価器の編集距離上限に収まる区間へ分けます。

標準出力には manifest・予測ファイルの SHA-256、split、モデル名・版、評価器版、集計件数と指標だけを出し、歌詞本文・sample ID・個別結果は出しません。モデル名・版は予測ファイルの申告値で、実行済みモデルの実体を検証しません。結果も非公開データとして扱い、公開 CI artifact へ載せません。実録歌唱の品質判断には人手で権利・注釈・録音条件を確認する必要があります。
