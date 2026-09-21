# Implementation Plan

## 方針

実装は機能単位で分割し、各機能には対応するEvaluator / testを同時に用意する。

0.1.x系は細かく分割してよい。1.0.0では要件で定義した主要機能を一通り実装し、Release Evaluationを通過する。

## 実装順序

1. Repository / build基盤
2. Rust Core API / Job基盤
3. Song Data schema / persistence
4. Audio normalization
5. Python Analysis Service
6. Stem Separation
7. Whisper lyrics
8. Alignment
9. F0 detector interface / ensemble
10. Vocal Event / Boundary Fusion
11. High-accuracy Score Generation
12. Rust Audio Engine / CPAL / WASAPI
13. Microphone monitoring / Mixer / Effects
14. Realtime Pitch / Scoring
15. Avalonia GUI shell
16. Library / analysis progress UI
17. Karaoke playback UI
18. Model package management
19. CI Evaluators / regression gates
20. 1.0.0 Release Evaluation

## Issueの原則

各Issueは可能な限り以下を含める。

- Purpose
- Scope
- Out of scope
- Acceptance criteria
- Tests / Evaluator
- Dependencies

機能実装とEvaluatorを分離しすぎず、少なくとも基本的な回帰テストは同じIssueで用意する。

大型の精度改善は別Issueへ分ける。
