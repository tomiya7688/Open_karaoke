# Quality Evaluation and CI

## 基本方針

機能を1つ実装したら、その機能の品質を自動評価するEvaluatorを同時に用意する。

機能コードだけを先行させない。

## CI階層

### Fast CI

PR / commitごとに実行する。

- static analysis
- lint
- unit tests
- API contract tests
- 小規模固定データによるEvaluator
- deterministic regression tests

### Model / Accuracy CI

モデル・解析ロジック変更時に実行する。

- stem quality
- lyric CER / WER
- alignment error
- pitch accuracy
- note onset / offset / pitch F1
- scoring monotonicity
- latency / synchronization

### Release Evaluation

1.0.0正式リリース前に大きな固定評価セットで実行する。

通常CIより重くてよい。

評価結果をバージョンごとに保存し、回帰を比較可能にする。

## Evaluator Interface

各Evaluatorは最低限以下を出力する。

```json
{
  "evaluator": "example",
  "version": 1,
  "dataset_version": 1,
  "metrics": {},
  "passed": true
}
```

CIはpass/failだけでなくmetricsをartifactとして保存する。

## Job APIとの統合

EvaluatorはGUIを使用せずJob APIから解析機能を呼び出せるようにする。

これにより以下を可能にする。

- バッチ精度測定
- 同一入力の再現テスト
- モデル比較
- 回帰検出
- 障害再現
- benchmark automation
