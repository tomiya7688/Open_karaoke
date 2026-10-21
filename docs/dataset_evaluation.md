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

`path` は `/` 区切りで、親ディレクトリ移動・絶対パス・manifest ディレクトリ外へのシンボリックリンクを拒否します。同じ `source_group` または同一音源 hash を development と holdout に分けることも拒否します。実録歌唱の品質や注釈の意味・時刻精度はこの段階では評価しません。

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

ローカルで `python -m open_karaoke_analysis.dataset_manifest datasets/manifest.json` を実行します。成功時は件数と hash 検査結果だけを表示します。公開 CI では private dataset を取得せず、検査結果や実データを公開 artifact にアップロードしません。モデルの版・実行条件・CER 等の測定結果との結合は #18 と #42 の後続単位です。
