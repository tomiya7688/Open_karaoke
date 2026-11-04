# Analysis quality and karaoke UX design

> **Proposed architecture and product acceptance criteria** as of `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` (2026-11-04), Issue #106.
>
> It is not a claim that quality thresholds, auto-reanalysis, installer or real-device UX are implemented. Existing evidence: [Full-song analysis](full_song_analysis.md), [Dataset evaluation](dataset_evaluation.md), [Testing](testing.md), [Desktop client](desktop_client.md), [Model Plugins](model_plugins.md).

## Product objective and constraints

The expected experience is: **choose a song → prepare with the least required work → sing → understand feedback**. Normally no manual lyric/note correction, model installation by hand or audio developer knowledge should be required.

Keep these independent:

1. **Integrity:** Is the artifact valid (source hash, timing, schema, language and model identity)?
2. **Estimated quality:** How trustworthy is this segment for the intended use? Derived model confidences and heuristic fusion scores are not necessarily calibrated.
3. **User action:** Can we sing now? Should one uncertain interval be retried? What can the user do if the result is poor?

Do not collapse these into an invented global “95% accurate” label. A synthetic regression pass or successfully downloaded model weight is **not** real-singing quality evidence.

## Actual pipeline and missing quality feedback loop

Current full-song pipeline is one sequential `analysis.song` Job:

```text
normalized 48kHz WAV
   → UMX-HQ stems
   → Whisper lyric candidates
   → Japanese CTC lyric alignment
   → YIN/NACF offline F0
   → Vocal Event boundary fusion
   → note candidate optimization
   → SongBundle
   → separate song.import Job
```

This already checks source hashes, format, time ranges and artifact ownership. **It does not yet re-run low-quality sections, automatically calibrate confidence, or choose an alternate path for a MIDI/ready-made instrumental input.** A failure at a required stage currently fails the Job; an inaccurate-but-valid result can pass structural tests.

The target pipeline adds a **QualityAssessor / RepairPlanner** after each stage and again before song publication:

```text
stage artifacts (immutable)
     │
     ├── hard validation ── integrity failure → fail, do not publish
     │
     └── quality assessment ── acceptable → continue
                    │
                    └── uncertain → bounded retry / alternative evidence
                                 │
                                 └── still uncertain → best-effort playable
                                     with transparent warnings, not fake scores
```

The RepairPlanner must be deterministic for a fixed artifact set and configuration. It must not invoke arbitrary new models or unbounded recursive retries. Keep audio/lyrics/notes evidence as distinct versioned objects. Do not let text fluency alone overwrite unusual but acoustically plausible lyrics.

## Quality report contract (design proposal)

Store optional quality metadata as a **separate versioned artifact** to avoid silently changing v1 `song.json`/`notes.json`. A draft layout:

```json
{
  "quality_report_version": 1,
  "song_analysis_fingerprint": "<verified hash>",
  "model_snapshots": [
    {
      "role": "lyrics",
      "adapter_id": "whisper-large-v3",
      "version": "<pinned version>",
      "weight_sha256": "<verified hash>"
    }
  ],
  "segments": [
    {
      "start_sample": 48000,
      "end_sample": 144000,
      "dimension": "lyric_alignment",
      "status": "needs_review",
      "signals": {
        "alignment_coverage": 0.62
      },
      "signal_interpretation": "diagnostic_not_calibrated",
      "flags": ["ctc_fallback"],
      "attempts": 1
    }
  ],
  "overall": "playable_with_warnings"
}
```

The sample values above are illustrative, **not measured project statistics**. Report should also carry algorithm/config versions, input/annotation provenance, successful/failed attempts, and effective model-selection source. `quality_report` is **not a legal or scientific certification**.

Recommended state values:

- `blocked`: corrupted/untrusted artifact or required playable assets missing. Do not publish as ready.
- `needs_review`: valid artifact, but some observations are uncertain. Keep evidence; bounded repair may improve it.
- `playable_with_warnings`: audio and timeline are safe to use, but some generated content is approximate.
- `ready`: all required structural contracts pass **and** any defined product gates pass. This is not equivalent to perfect lyrics or score.
- `not_evaluated`: no quality measurement, distinguish from failure and from perfect zero error.

Avoid conflating `confidence` with correctness probability. Require `signal_interpretation` such as `heuristic`, `calibrated`, `measured` on each signal where important.

### Proposed quality signals

| Stage | Diagnostic evidence | User impact / retry choice |
|---|---|---|
| Stems | Vocal energy leakage, estimated masking, clip/nonfinite, reconstruction error (structural only) | High leakage may pollute mic monitoring and ASR; offer model comparison when licensed |
| Whisper ASR | Coarse segments, no-speech flags, repeated hallucination patterns, language agreement, reference text difference | Re-decode only uncertain windows with context; never replace unique lyric on grammar grounds |
| Alignment | Character/line span coverage, missing/OOV characters, score/time discrepancy, duration overlap | Re-align an expanded window or mark line timing approximate |
| F0 | YIN/NACF agreement, octave alternatives, voicing consistency, gap and harmonic indicators | Re-estimate suspicious windows, retain original evidence and detector outputs |
| Vocal Event | Acoustic/voicing/lyric boundary agreement; uncertain legato/melisma segmentation | Consider neighboring windows and lyric evidence, avoid splitting vibrato |
| Note generation | Note onset/offset uncertainty, short fragment count, chromatic soft-prior conflict, reference MIDI differences | Re-score candidate timeline and local optimization, retain alternatives |

**Hard failures** (hash mismatch, invalid PCM, incompatible schema) are not “low confidence”. They must fail safely; retries must not hide provenance mismatch. **Data quality warnings** should not automatically prevent singing.

## Bounded automatic repair

- Set deterministic per-stage and whole-song budgets: max attempts, max window duration, time/CPU/VRAM budget, cancellation checkpoints. No indefinite search for a “perfect” result.
- Record attempt identity and exact artifact dependencies; keep original observations. If a window is reanalyzed, regenerate only downstream artifacts whose input hashes actually changed.
- Compare candidates against an objective for each dimension. Preserve exact lyric text, character alignment and note timing separately; don't reward grammatical fluency as “song accuracy”.
- Prefer conservative non-destructive fallbacks: provide lyric line timing without invented syllable times; display pitch bar only where notes are valid; allow unscored/practice mode when score evidence is weak.
- Allow optional user-provided lyric / MIDI to increase evidence. They are references, not always ground truth. A source version/offset/tempo mismatch must surface a warning.
- Do not call heavyweight Python from realtime playback callbacks. Offline reanalysis may run after song playback if #43 decides it would threaten audio latency.

## Model selection and reproducibility

The model UI currently records a **role-level selection** and can resolve it for individual analysis Jobs. Full-song `SongAdapter` selects internal stages from `options.models` or the registry default, not from stored role selections. Also, a package manifest can identify a registered backend without loading distinct weights. Therefore **“package available” does not mean the weights in its folder are in use**.

Before full-song analysis starts, create an **effective model plan snapshot**:

```text
request explicit model/override
    > valid stored selection
    > product default
```

Per stage capture requested ID, actual adapter, model artifact/weight checksum, backend/runtime version, expected language, device, options, and reason for any fallback. Pass the plan to the Job as immutable input; do not resolve defaults again in later stages. If a previously selected model disappears, default fallback may be allowed for ordinary use **only with an explicit UI notice**; an evaluator or reproducibility mode must **fail closed** rather than silently compare different models.

User-installed MOD licenses are **not** blocked by model loader policy; this is separate from official distribution screening. But third-party Python code is executable and not inherently sandboxed. A manifest's readability/availability must not be presented as runtime safety verification.

## Accuracy evaluation gates and realistic data

The project already has dataset manifest validation, synthetic CER regressions and a private-data reporting interface. That is valuable infrastructure, but much of the accuracy evidence remains **synthetic**. `kind=lyrics` annotations are currently the more developed path; real-singing datasets and cross-task reference annotations are still work under #42/#18.

Required comparisons:

| Dimension | Metrics / diagnostic | Important stratification |
|---|---|---|
| Source separation | SI-SDR/SDR and human listening panel | Solo/chorus, instrumental density, reverb, duet, bleed |
| Lyrics | CER, deletion/hallucination rate, repetition behavior | Japanese/other languages, lyrics style, breaths, fast passages |
| Lyric alignment | Boundary median/p95 milliseconds, coverage and missing lines | Held vowels, melisma, repeated lyrics, backing leakage |
| Pitch | Cent median/p95, voicing precision/recall, octave error | Low/high voices, falsetto, noise, instrument bleed |
| Note generation | Pitch/onset/offset F1, over/under segmentation, inter-rater agreement | Legato, vibrato, scoops, rap, chromatic passages |
| Realtime scoring | Monotonicity, timing fairness, per-note reproducibility, correlation with blinded human ratings | Mic/interface types, USB latency, singers, genres |
| User experience | Time-to-first-song, import success, first-run failures, device recoverability, user edits required | CPU-only/low RAM, no network, high DPI, accessible input |

- Split by song/recording/singer/session **groups**, not random tiny windows, to avoid leakage from repeat choruses or near-duplicate audio.
- Keep `development` and `holdout` independent; don't tune to holdout repeatedly. Version inputs, annotations, model weights, options and thresholds.
- Use rights-cleared recordings/annotations and explicit publication policy. Do **not** upload private music, raw vocals, lyrics or ID-bearing individual reports as public CI artifacts.
- Synthetic fixtures are used for fast deterministic regressions; actual model-load smoke is separate; human-annotated recordings are used for threshold calibration; Windows physical-device tests are the final product gate.
- Define per-metric candidate thresholds only after a baseline dataset exists. Do **not** invent a “production-grade” CER/F1/latency target in advance.

## First-run and daily-use UX (proposed)

### First launch (no developer tooling exposed to ordinary users)

1. Check packaged Rust/Core/Python runtime and installed models locally. Show **installed and verified**, **download available**, **runtime missing**, **unsupported**, **user-provided/unverified** as distinct states.
2. Ask which microphone and output device to use; play a safe low-volume test, then show an input level/VU meter. If available, measure input/output path delays; otherwise mark `uncalibrated` and offer practice mode. Do not suggest that 0 ms was measured.
3. Show available disk space, approximate model footprint (from actual package manifest), network/offline requirements and progress/cancel before downloading. An offline/manual model package remains optional.
4. Save device profiles per stable physical device ID + buffer/rate/backend; names alone are not reliable and duplicate-name devices are possible. Device changes invalidate measured latency.
5. Allow skipping optional fine-tuning. First-run setup must not become an obstacle to playing a supplied accompaniment + MIDI score.

### Song import, analysis and playback

```text
Choose local song / accompaniment
    → optional existing lyrics or MIDI
    → preflight media, devices, models and space
    → prepare (stage progress + cancellable task)
    → ready / ready with warnings / input action required
    → sing (one-click)
    → result with explainable per-note metrics
    → optional edit/retry the uncertain passage
```

- Support two production paths: **auto-generated** song and **supplied accompaniment + MIDI/lyrics**. Current MIDI importer and core SongStore should be reused, not rewritten inside GUI.
- A multi-minute analysis progress UI should identify the stage, model, input errors and last durable cache checkpoint. Only show an ETA if measured empirically; never derive one solely from a nominal percentage.
- Show **why** a song is not ready and the one action that fixes it (e.g. missing Python runtime, missing weight, bad audio format, no microphone). Transport/network retry must not re-import or double-register a song.
- Distinguish expected content warnings from blocking errors. Keep user-edited lyrics/notes in a separate versioned layer so optional reanalysis does not overwrite edits.
- During karaoke, show one clear current/next lyric pair, stable high-contrast pitch bars, a distinct singer pitch trace, and controls for play/pause, mic/music/effect gain and seek. Avoid dense diagnostics on the singing screen.
- On-device audio health indicators (clip/low input/overflow) must be actionable and not masquerade as singing mistakes. Show scoring as **provisional** when chart or latency calibration is unverified.
- Responsive layout must account for high DPI, small/larger stage displays, keyboard input, dark mode and high contrast. Headless UI screenshots are useful but not a substitute for real Windows accessibility/IME/device testing.

### Failure and cancellation behavior

| Failure | Expected UI/core behavior |
|---|---|
| Model missing/unavailable | Offer installed official-compatible option or safe setup route; do not quietly call a different model in reproducibility mode |
| Analysis stage error | Preserve earlier **verified, immutable cache** if valid, but do not publish a partial SongBundle as complete |
| Very low-confidence lyric/note section | Offer local retry or continue playing with quality warning, avoid blocking the full song |
| Device disconnected | Pause session with explicit reason; reselect device, re-anchor clock and invalidate calibration |
| Repeated seek/restart | Epoch-scoped pitch and score frames dropped; UI returns to authoritative Core position |
| No reliable score available | Allow practice playback with pitch bars where reliable; don't award fictitious grade |
| Disk full / interrupted import | Don't corrupt SongStore; make retry/resume conditions explicit |
| Core restart | Lost in-memory Job not shown as completed; reconnect/reauthenticate via #41, retain persisted songs |

## Acceptance and implementation order

**P1:** Complete actual song registration from arbitrary WAV/MP3/MP4 import (#36); define Core-owned karaoke session; realtime pitch/scoring (#14); real-singing dataset and objective quality gates (#42/#18); measured latency method (#40).

**P2:** Integrate the mixer and device settings (#13), UI playback and current/next lyric / pitch trace (#16), effective model-plan snapshot and honest status (#17), resource isolation (#43), packaging (#39), first-run device preflight. The later editable lyric/note editor (#38) remains optional in the normal flow but a 1.0.0 requirement.

**P3:** Explore multi-ASR, song-global reanalysis or LoRA (#44) only after the simpler objective baselines expose where current models fail.

Other design risks: Japanese-only initial CTC aligner despite multilingual Whisper; a single model cannot assume every supported song language, and fallback must never fabricate timings. Name-based WASAPI device selection cannot reliably survive replug. Evaluate confidence before using it to declare precision; keep the GUI/score consistent with the chosen annotation and analysis snapshot.

## #108 実装詳細設計：品質判定と限定再解析

この節は#108の**実装前の正本案**。既存の全曲 `SongAdapter`、個別Adapter、
`song.import`の契約を参照しながら設計を確定する。数値閾値や候補モデルの有効性は
実録データで校正するまで`Unverified`。実装がこの契約と異なった場合は
**その差分を同じPRで本節へ反映**し、IssueのClose記録で代用しない。

### 解析成果物の依存DAG

現在の既定実行順は`stems → lyrics → alignment → pitch → events → notes`だが、
再計算の**依存関係**は実行順そのものと異なる。

```text
original (48 kHz WAV)
       │
     stems ──────── vocals ───────────────────┐
       │             ├── lyrics → alignment ─┤
       │             └── pitch ────────────┐  │
       │                                   v  v
       └── accompaniment                events
                                          │
                    alignment ────────────┼──> notes
                    pitch ────────────────┘
```

`notes`のオプション/参照候補も依存キーへ含める。実際には`events`も
音声そのものを特徴量抽出に使用するので、`vocals`を必須の依存とする。
全stageの再実行を毎回行うのではなく、**入力artifactの内容hashが変わった下流ノードだけ無効化**する。

| 変更されたevidence | 再実行対象の最小集合（候補） |
|---|---|
| stemsのvocals | lyrics, alignment, pitch, events, notes |
| ASR lyrics | alignment, events, notes。pitchの入力が同じなら再実行しない |
| alignment | events, notes |
| pitch | events, notes |
| events | notes |
| note candidates/参照MIDI | notesのみ（候補がevents入力へも供給される設計ならeventsも） |

同じ曲の連続する二つの区間で再解析するときは、境界の共有部分と前後文脈を
明示した`local_sample → song_sample`写像を用い、隣区間を不意に上書きしない。
パスの名前だけで同一性判定をせず、
`{source_sha256, model_id, model_version, weight_sha256, options_digest, parent_artifact_hashes}`
をevidence identityの材料とする。digestはキー順など正規化方式を版で固定する。

### 型・関数・責務（新設する設計契約）

| 型/コンポーネント | 入力 | 出力 | 責務 |
|---|---|---|---|
| `ArtifactDependencyGraph` | stage role、入力sha、model/config snapshot | immutable node IDと下流依存 | 再解析後の無効化対象を一意に決定 |
| `QualityAssessor` | 検証済みstage artifactと観測evidence | `QualityFinding[]` | **純粋判定**。データの読込・モデル推論・再実行はしない |
| `RepairPlanner` | findings、dependency graph、model plan、budget | `RepairPlan` | 限定再実行するロール・区間・入力・budgetを決定 |
| `RepairExecutor` | immutable plan、既存AdapterRegistry、Job Cancellation | 新しい版のartifactとattempt結果 | 非callbackのPython worker内で実行 |
| `QualityPublisher` | 完全性検査済みartifact graphと最終findings | `quality_report.json` | 監査可能な結果を一括公開。壊れた曲はreadyにしない |

**継承ではなく委譲**。既存の`ModelAdapter` Protocolは変更しない。
各モデルが品質判定まで継承して抱える形にはせず、`QualityAssessor`から
独立したdetector（`LyricRepeatDetector`, `PitchDisagreementDetector`等）の
関数へ処理を委譲する。すべての役割に同じ「confidence」の意味を強制しない。

必要最小のデータ契約案:

```text
QualityFinding:
  artifact_id, stage, span_start_sample, span_end_sample,
  diagnostic_code, severity, signal_name, signal_value,
  calibration: "heuristic" | "calibrated" | "measured" | "unknown",
  evidence_artifact_ids[]

RepairPlan:
  plan_version, input_fingerprint, selected_nodes[],
  windows[{start_sample,end_sample,context_before,context_after}],
  model_plan_snapshot, max_attempts, max_total_attempts,
  downstream_invalidation[], cancellation_policy

QualityReport:
  report_version, song_fingerprint, parent_artifact_hashes[],
  stage_findings[], applied_repairs[], skipped_repairs[],
  output_readiness, unverified_reasons[]
```

内部は型付きモデルとして検証し、JSONへの変換時は`NaN`/`Infinity`・
重複キー・不正sample span・未知の親artifactを拒否する。
JSONファイルのパスだけを新しいモデルへ自由入力として渡さない。
`report_version`は、既存の`song.json`/`notes.json`の`format_version`と独立にする。

### 審査→計画→実行→公開の状態機械

```text
StageValidating
  ├── structural failure → Failed (公開なし)
  └── valid → QualityAssessing
                   ├── no finding / no calibrated gate → Publishing
                   └── findings → Planning
                                      ├── no compatible repair / budget depleted → PublishingWithWarnings
                                      └── plan → Executing → ValidatingRepaired
                                                       ├── invalid artifact → Failed (新成果物は不採用)
                                                       └── valid → Reassess → Publishing
```

- `quality: not_evaluated`は`ready`とも`blocked`とも異なる。
  heuristicの閾値が低いだけで`blocked`にして曲を消さない。
- 壊れたhash/Schema/PCMは**hard integrity failure**であり、
  「気になる歌詞」を再解析する品質警告と混ぜない。
- 1回の`analysis.song`実行で許可する再解析回数・区間長・CPU/VRAM・
  経過時間の上限は`RepairBudget`へ設定する。値は実データで調整し、未測定の最適値を装わない。
  制限到達は`skipped_repairs(reason=budget_exhausted)`として残す。
- 計画は`input_fingerprint`と設定が同じなら決定的であるべきだが、
  実行時間制限により終了した場合は同一結果になると保証しない。
  比較評価モードでは任意のモデル自動fallbackを禁止する。
- 再解析で前段を更新しても**SongStoreに既に確定した曲を上書きしない**。
  一時Job領域へ新しい候補を作り、全下流の検査に成功したときだけ
  `song.import`経由で明示的な登録/版管理を行う。
- ユーザーの歌詞編集は`base analysis`とは独立したlayerとし、
  再解析でユーザーが修正した内容を自動で消さない（#38と契約を合わせる）。

### Vocal Event と Alignmentの二段階利用

現在の初回`alignment`は`events`が生成される前に動くため、
Closed #10が目指した「境界を歌詞整列へ戻す」接続がない。
一度に相互参照する循環依存を作らず、**pass別の新しいartifact**を使う。

```text
alignment/pass1 → events/pass1
                      │
           optional alignment/pass2
                      │
                events/pass2 → notes/pass2
```

`pass2`実施条件は`pass1`の診断が一定条件を満たす区間に限定する。
無限にpassを重ねず、開始sampleと終了sample、検出器versionと親hashを保存する。
歌詞本文を改変しない整列修正と、ASRテキストを変更する再解析は**別のrepair**とする。
音素・文字単位の正解境界がない実歌唱では、改善を証明したことにしない。

### 実装と同時に追加するテスト

| テスト（新設予定） | 確認内容 |
|---|---|
| `python/tests/test_quality_assessor.py` | 無音/反復/低voicing、機能別signalの意味、非有限値拒否 |
| `python/tests/test_repair_planner.py` | 上限、区間重なり、モデル変更、同一入力時の決定性、未対応repairの明示 |
| `python/tests/test_artifact_dependency_graph.py` | parent hash変更と下流無効化、境界マージ、別版の識別 |
| `python/tests/test_song.py`追加 | 最終成果物の不可変性、キャンセル、段階失敗、二段階passとno fake success |
| `python/tests/test_dataset_manifest.py`周辺 | development/holdout混在防止、権利参照のある実歌唱検証 |
| `core/tests/song_import.rs`追加 | 構造不正なquality reportを歌える曲として確定させない |

実装IssueとPRはここに書いた関数・データ・例外を**実コードとテストへ対応づける**。
修正の必要が判明したら、本節を実装中に更新する。

