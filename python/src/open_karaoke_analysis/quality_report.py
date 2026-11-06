"""Versioned quality report: separates quality warnings from analysis success (#108.1)."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

QUALITY_REPORT_FORMAT_VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}")
STAGES = ("stems", "lyrics", "alignment", "pitch", "events", "notes")
DIAGNOSTIC_CODES = (
    "whisper_repeat",
    "whisper_hallucination",
    "alignment_low_coverage",
    "boundary_inconsistent",
)
INTEGRITY_CODE = "integrity_violation"


class QualityReportError(ValueError):
    """
    {
      責務: [QualityReportError: 品質レポートの契約違反(fail closed)を表す]
      フィールド: []
    }
    """


class CalibrationState(StrEnum):
    """
    {
      責務: [CalibrationState: flagの根拠の強さ(hard_integrity/diagnostic/calibrated)を分離する]
      フィールド: []
    }
    """

    HARD_INTEGRITY = "hard_integrity"
    DIAGNOSTIC = "diagnostic"
    CALIBRATED = "calibrated"


class Playability(StrEnum):
    """
    {
      責務: [Playability: 解析成功とは別の再生可否。blockedは必須契約違反のみ]
      フィールド: []
    }
    """

    PLAYABLE = "playable"
    PLAYABLE_WITH_WARNINGS = "playable_with_warnings"
    BLOCKED = "blocked"


def _fail(message: str) -> QualityReportError:
    """
    {
      責務: [_fail: 例外を作る共通口]
      処理: [1: メッセージを包む]
      引数: [message: 原因]
      戻り値: [QualityReportError]
    }
    """
    return QualityReportError(message)


def _is_int(value: object) -> bool:
    """
    {
      責務: [_is_int: bool以外の整数か判定する]
      処理: [1: 型を厳密に比較する]
      引数: [value: 対象]
      戻り値: [整数ならTrue]
    }
    """
    return type(value) is int


def _is_num(value: object) -> bool:
    """
    {
      責務: [_is_num: 有限の実数(boolを除く)か判定する]
      処理: [1: 型, 2: 有限性]
      引数: [value: 対象]
      戻り値: [有限数ならTrue]
    }
    """
    return type(value) in (int, float) and math.isfinite(value)


def _hex(value: object, label: str, *, nullable: bool = False) -> None:
    """
    {
      責務: [_hex: SHA-256形式(64桁小文字16進数)を検証する]
      処理: [1: nullを許す場合は通す, 2: 形式を照合する]
      引数: [value: 対象, label: エラー表示名, nullable: nullを許すか]
      戻り値: []
      エラー: [QualityReportError: 形式不正]
    }
    """
    if value is None and nullable:
        return
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _fail(f"{label} must be a lowercase 64-hex sha256")


def _name(value: object, label: str) -> None:
    """
    {
      責務: [_name: 識別名(小文字英数字_.、64字以内)を検証する]
      処理: [1: 形式を照合する]
      引数: [value: 対象, label: エラー表示名]
      戻り値: []
      エラー: [QualityReportError: 形式不正]
    }
    """
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _fail(f"{label} must match {_NAME.pattern}")


def _keys(raw: object, expected: set[str], label: str) -> dict:
    """
    {
      責務: [_keys: オブジェクトのキー集合が契約と完全一致するか検証する]
      処理: [1: dict確認, 2: キー集合比較]
      引数: [raw: 対象, expected: 期待キー, label: エラー表示名]
      戻り値: [同じdict]
      エラー: [QualityReportError: 型・キー不一致]
    }
    """
    if not isinstance(raw, dict) or set(raw) != expected:
        raise _fail(f"{label} must be an object with exactly keys {sorted(expected)}")
    return raw


@dataclass(frozen=True)
class QualityOptions:
    """
    {
      責務: [QualityOptions: 版付きの診断閾値。options_sha256へ反映され比較可能にする]
      フィールド: [
        options_version: 閾値集合の版,
        repeat_ratio_min: この値以上でwhisper_repeat,
        hallucination_score_min: この値以上でwhisper_hallucination(0..1 heuristic),
        alignment_coverage_min: この値未満でalignment_low_coverage,
        boundary_overlap_samples_max: この値超の重なりでboundary_inconsistent
      ]
    }
    """

    options_version: int = 1
    repeat_ratio_min: float = 0.5
    hallucination_score_min: float = 0.8
    alignment_coverage_min: float = 0.6
    boundary_overlap_samples_max: int = 0

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: 閾値の範囲を検証する]
          処理: [1: 版, 2: 0..1の範囲, 3: 重なり上限が非負整数]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 範囲外]
        }
        """
        if not _is_int(self.options_version) or self.options_version < 1:
            raise _fail("options_version must be a positive integer")
        for label in ("repeat_ratio_min", "hallucination_score_min", "alignment_coverage_min"):
            value = getattr(self, label)
            if not _is_num(value) or not 0 <= value <= 1:
                raise _fail(f"{label} must be within 0..1")
        if not _is_int(self.boundary_overlap_samples_max) or self.boundary_overlap_samples_max < 0:
            raise _fail("boundary_overlap_samples_max must be a non-negative integer")

    def to_dict(self) -> dict:
        """
        {
          責務: [to_dict: 正規化可能な辞書にする]
          処理: [1: 全フィールドを列挙]
          引数: []
          戻り値: [辞書]
        }
        """
        return {
            "options_version": self.options_version,
            "repeat_ratio_min": self.repeat_ratio_min,
            "hallucination_score_min": self.hallucination_score_min,
            "alignment_coverage_min": self.alignment_coverage_min,
            "boundary_overlap_samples_max": self.boundary_overlap_samples_max,
        }

    def sha256(self) -> str:
        """
        {
          責務: [sha256: 閾値集合の正規JSONハッシュを返す]
          処理: [1: 正規JSON化, 2: SHA-256]
          引数: []
          戻り値: [64桁16進数]
        }
        """
        return _digest(self.to_dict())


@dataclass(frozen=True)
class ModelRef:
    """
    {
      責務: [ModelRef: 実際に動いたmodel/weights/optionsの参照。未公開値はnull(推測しない)]
      フィールド: [
        model_id: 識別名, model_version: 版文字列,
        weights_sha256: 重みhashまたはnull, options_sha256: オプションhash
      ]
    }
    """

    model_id: str
    model_version: str
    weights_sha256: str | None
    options_sha256: str

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: 形式を検証する]
          処理: [1: id, 2: 版, 3: hash]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 形式不正]
        }
        """
        _name(self.model_id, "model_id")
        if not isinstance(self.model_version, str) or not 0 < len(self.model_version) <= 64:
            raise _fail("model_version must be a 1..64 char string")
        _hex(self.weights_sha256, "weights_sha256", nullable=True)
        _hex(self.options_sha256, "options_sha256")

    def to_dict(self) -> dict:
        """
        {
          責務: [to_dict: 辞書にする]
          処理: [1: 全フィールド列挙]
          引数: []
          戻り値: [辞書]
        }
        """
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "weights_sha256": self.weights_sha256,
            "options_sha256": self.options_sha256,
        }


@dataclass(frozen=True)
class InputRef:
    """
    {
      責務: [InputRef: 評価した入力artifactの名前・版・内容hash]
      フィールド: [artifact: 名前, format_version: そのartifactの版, sha256: 内容hash]
    }
    """

    artifact: str
    format_version: int
    sha256: str

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: 形式を検証する]
          処理: [1: 名前, 2: 版, 3: hash]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 形式不正]
        }
        """
        _name(self.artifact, "artifact")
        if not _is_int(self.format_version) or self.format_version < 1:
            raise _fail("input format_version must be a positive integer")
        _hex(self.sha256, "input sha256")

    def to_dict(self) -> dict:
        """
        {
          責務: [to_dict: 辞書にする]
          処理: [1: 全フィールド列挙]
          引数: []
          戻り値: [辞書]
        }
        """
        return {
            "artifact": self.artifact,
            "format_version": self.format_version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Attempt:
    """
    {
      責務: [Attempt: 処理出典。初回か再解析か、親レポートとの関係]
      フィールド: [
        pass_index: 0始まりの試行番号, kind: initial|repair,
        parent_report_sha256: repair時の親hash
      ]
    }
    """

    pass_index: int = 0
    kind: str = "initial"
    parent_report_sha256: str | None = None

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: 試行種別と親hashの整合を検証する]
          処理: [1: 番号, 2: 種別, 3: repairは親必須・initialは親なし]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 不整合]
        }
        """
        if not _is_int(self.pass_index) or self.pass_index < 0:
            raise _fail("pass_index must be a non-negative integer")
        if self.kind not in ("initial", "repair"):
            raise _fail("attempt kind must be initial or repair")
        _hex(self.parent_report_sha256, "parent_report_sha256", nullable=True)
        if (self.kind == "repair") != (self.parent_report_sha256 is not None):
            raise _fail("repair attempts require a parent hash and initial ones must not have one")
        if (self.kind == "initial") != (self.pass_index == 0):
            raise _fail("pass_index 0 is the initial attempt and repairs start at 1")

    def to_dict(self) -> dict:
        """
        {
          責務: [to_dict: 辞書にする]
          処理: [1: 全フィールド列挙]
          引数: []
          戻り値: [辞書]
        }
        """
        return {
            "pass_index": self.pass_index,
            "kind": self.kind,
            "parent_report_sha256": self.parent_report_sha256,
        }


@dataclass(frozen=True)
class QualityFlag:
    """
    {
      責務: [QualityFlag: 1区間の品質指摘。heuristic_scoreは確率ではない0..1の目安]
      フィールド: [
        code: 一意コード, stage: 対象段, start_sample/end_sample: 48 kHz整数の半開区間,
        diagnostic: 診断名, calibration_state: 根拠の強さ, calibration_id: calibrated時のみ,
        heuristic_score: 0..1の目安(hard_integrityは持てない), value/threshold: 測定値と閾値(任意)
      ]
    }
    """

    code: str
    stage: str
    start_sample: int
    end_sample: int
    diagnostic: str
    calibration_state: CalibrationState
    calibration_id: str | None = None
    heuristic_score: float | None = None
    value: float | None = None
    threshold: float | None = None

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: コード・範囲・校正状態の整合を検証する]
          処理: [1: 段とコード, 2: sample範囲, 3: 校正状態ごとの制約, 4: 数値]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 不整合]
        }
        """
        if self.stage not in STAGES:
            raise _fail(f"unknown stage: {self.stage}")
        if self.code not in (*DIAGNOSTIC_CODES, INTEGRITY_CODE):
            raise _fail(f"unknown flag code: {self.code}")
        _name(self.diagnostic, "diagnostic")
        if not _is_int(self.start_sample) or not _is_int(self.end_sample):
            raise _fail("sample range must be integer 48 kHz sample indices")
        if not 0 <= self.start_sample < self.end_sample:
            raise _fail("sample range must satisfy 0 <= start < end")
        if not isinstance(self.calibration_state, CalibrationState):
            raise _fail("calibration_state must be a CalibrationState")
        hard = self.calibration_state is CalibrationState.HARD_INTEGRITY
        if hard != (self.code == INTEGRITY_CODE):
            raise _fail("integrity_violation and hard_integrity must go together")
        if (self.calibration_state is CalibrationState.CALIBRATED) != (
            self.calibration_id is not None
        ):
            raise _fail("calibration_id is required for and only for calibrated flags")
        if self.calibration_id is not None:
            _name(self.calibration_id, "calibration_id")
        if hard and self.heuristic_score is not None:
            raise _fail("hard_integrity flags cannot carry a heuristic_score")
        if self.heuristic_score is not None and not (
            _is_num(self.heuristic_score) and 0 <= self.heuristic_score <= 1
        ):
            raise _fail("heuristic_score must be within 0..1")
        for label in ("value", "threshold"):
            item = getattr(self, label)
            if item is not None and not _is_num(item):
                raise _fail(f"{label} must be a finite number or null")

    def sort_key(self) -> tuple:
        """
        {
          責務: [sort_key: 決定的な並びのキーを返す]
          処理: [1: 範囲・段・コード・診断名の順]
          引数: []
          戻り値: [タプル]
        }
        """
        return (self.start_sample, self.end_sample, self.stage, self.code, self.diagnostic)

    def to_dict(self) -> dict:
        """
        {
          責務: [to_dict: 辞書にする]
          処理: [1: 全フィールド列挙]
          引数: []
          戻り値: [辞書]
        }
        """
        return {
            "code": self.code,
            "stage": self.stage,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "diagnostic": self.diagnostic,
            "calibration_state": self.calibration_state.value,
            "calibration_id": self.calibration_id,
            "heuristic_score": self.heuristic_score,
            "value": self.value,
            "threshold": self.threshold,
        }


def _derive(flags: tuple[QualityFlag, ...]) -> tuple[Playability, list[str]]:
    """
    {
      責務: [_derive: flagから再生可否と警告コード一覧を導出する]
      処理: [
        1: hard_integrityがあればblocked,
        2: 診断のみならplayable_with_warnings,
        3: 無ければplayable
      ]
      引数: [flags: 全flag]
      戻り値: [(playability, 重複なしソート済みコード)]
    }
    """
    codes = sorted({flag.code for flag in flags})
    if any(flag.calibration_state is CalibrationState.HARD_INTEGRITY for flag in flags):
        return Playability.BLOCKED, codes
    return (Playability.PLAYABLE_WITH_WARNINGS if flags else Playability.PLAYABLE), codes


@dataclass(frozen=True)
class QualityReport:
    """
    {
      責務: [QualityReport: 版付き品質レポート。解析の成功/失敗は表さず再生可否と警告だけを持つ]
      フィールド: [
        assessor: 評価器のmodel/weights/options, options: 閾値, attempt: 処理出典,
        inputs: 入力artifact(名前順), stage_models: 段名→ModelRef(段順),
        flags: 正規ソート済みflag
      ]
    }
    """

    assessor: ModelRef
    options: QualityOptions
    attempt: Attempt
    inputs: tuple[InputRef, ...]
    stage_models: tuple[tuple[str, ModelRef], ...]
    flags: tuple[QualityFlag, ...]

    def __post_init__(self) -> None:
        """
        {
          責務: [__post_init__: 並び・重複・assessorのoptions hash整合を検証する]
          処理: [
            1: inputsの名前重複と順序, 2: 段の重複と順序,
            3: flagの順序と段, 4: options hash一致
          ]
          引数: []
          戻り値: []
          エラー: [QualityReportError: 不整合]
        }
        """
        names = [item.artifact for item in self.inputs]
        if names != sorted(set(names)):
            raise _fail("inputs must be unique and sorted by artifact")
        stages = [stage for stage, _ in self.stage_models]
        if len(set(stages)) != len(stages) or stages != [s for s in STAGES if s in stages]:
            raise _fail("stage_models must be unique, known and in stage order")
        if list(self.flags) != sorted(self.flags, key=QualityFlag.sort_key):
            raise _fail("flags must be in canonical order")
        if self.assessor.options_sha256 != self.options.sha256():
            raise _fail("assessor options_sha256 does not match options")

    @property
    def playability(self) -> Playability:
        """
        {
          責務: [playability: 導出された再生可否]
          処理: [1: flagから導出]
          引数: []
          戻り値: [Playability]
        }
        """
        return _derive(self.flags)[0]

    def body_dict(self) -> dict:
        """
        {
          責務: [body_dict: report_sha256を除く本体の辞書を返す]
          処理: [1: 各部を辞書化, 2: 導出値を添える]
          引数: []
          戻り値: [辞書]
        }
        """
        playability, codes = _derive(self.flags)
        return {
            "format_version": QUALITY_REPORT_FORMAT_VERSION,
            "assessor": self.assessor.to_dict(),
            "options": self.options.to_dict(),
            "attempt": self.attempt.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "stage_models": [{"stage": stage, **ref.to_dict()} for stage, ref in self.stage_models],
            "flags": [flag.to_dict() for flag in self.flags],
            "playability": playability.value,
            "warning_codes": codes,
        }

    def sha256(self) -> str:
        """
        {
          責務: [sha256: 本体の正規JSON hash(report_sha256)を返す]
          処理: [1: body_dictをハッシュ]
          引数: []
          戻り値: [64桁16進数]
        }
        """
        return _digest(self.body_dict())


def _canonical(value: object) -> str:
    """
    {
      責務: [_canonical: キー順固定・NaN拒否の正規JSON文字列を返す]
      処理: [1: sort_keys, 区切り固定, allow_nan=False]
      引数: [value: JSON化可能な値]
      戻り値: [文字列]
    }
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value: object) -> str:
    """
    {
      責務: [_digest: 正規JSONのSHA-256を返す]
      処理: [1: 正規化, 2: UTF-8でハッシュ]
      引数: [value: JSON化可能な値]
      戻り値: [64桁16進数]
    }
    """
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def report_to_json(report: QualityReport) -> str:
    """
    {
      責務: [report_to_json: 正規JSONへ直列化する(同じ入力+設定で同じバイト列)]
      処理: [1: 本体にreport_sha256を足す, 2: 正規JSON化]
      引数: [report: レポート]
      戻り値: [JSON文字列]
    }
    """
    body = report.body_dict()
    body["report_sha256"] = _digest(body)
    return _canonical(body)


def report_from_json(text: str) -> QualityReport:
    """
    {
      責務: [report_from_json: JSONを検証して読み込む(版・キー・hash・導出値が不正なら拒否)]
      処理: [1: 解析, 2: 版とキー集合, 3: 各部を型へ復元, 4: 導出値とreport_sha256を再計算し照合]
      引数: [text: JSON文字列]
      戻り値: [QualityReport]
      エラー: [QualityReportError: 契約違反]
    }
    """
    try:
        raw = json.loads(text, parse_constant=_reject_constant)
    except ValueError as error:
        raise _fail(f"invalid json: {error}") from error
    keys = {
        "format_version", "assessor", "options", "attempt", "inputs", "stage_models",
        "flags", "playability", "warning_codes", "report_sha256",
    }  # fmt: skip
    raw = _keys(raw, keys, "quality_report")
    if not _is_int(raw["format_version"]) or raw["format_version"] != QUALITY_REPORT_FORMAT_VERSION:
        raise _fail("unsupported quality_report format_version")
    _hex(raw["report_sha256"], "report_sha256")
    if not all(
        isinstance(raw[k], list) for k in ("inputs", "stage_models", "flags", "warning_codes")
    ):
        raise _fail("inputs, stage_models, flags and warning_codes must be arrays")
    model_keys = {"model_id", "model_version", "weights_sha256", "options_sha256"}
    try:
        options = QualityOptions(
            **_keys(raw["options"], set(QualityOptions().to_dict()), "options")
        )
        report = QualityReport(
            assessor=ModelRef(**_keys(raw["assessor"], model_keys, "assessor")),
            options=options,
            attempt=Attempt(**_keys(raw["attempt"], set(Attempt().to_dict()), "attempt")),
            inputs=tuple(
                InputRef(**_keys(item, {"artifact", "format_version", "sha256"}, "input"))
                for item in raw["inputs"]
            ),
            stage_models=tuple(_stage_model(item, model_keys) for item in raw["stage_models"]),
            flags=tuple(_flag(item) for item in raw["flags"]),
        )
    except TypeError as error:
        raise _fail(f"malformed quality_report: {error}") from error
    playability, codes = _derive(report.flags)
    if raw["playability"] != playability.value or raw["warning_codes"] != codes:
        raise _fail("derived playability/warning_codes do not match flags")
    if raw["report_sha256"] != report.sha256():
        raise _fail("report_sha256 mismatch")
    return report


def _reject_constant(name: str) -> None:
    """
    {
      責務: [_reject_constant: NaN/Infinityリテラルを拒否する]
      処理: [1: 例外を投げる]
      引数: [name: 定数名]
      戻り値: []
      エラー: [QualityReportError: 常に]
    }
    """
    raise _fail(f"non-finite number is not allowed: {name}")


def _stage_model(item: object, model_keys: set[str]) -> tuple[str, ModelRef]:
    """
    {
      責務: [_stage_model: stage_models要素を(段, ModelRef)へ復元する]
      処理: [1: キー検証, 2: stageを分離してModelRef化]
      引数: [item: JSON要素, model_keys: ModelRefのキー]
      戻り値: [(stage, ModelRef)]
      エラー: [QualityReportError: 契約違反]
    }
    """
    data = dict(_keys(item, model_keys | {"stage"}, "stage_model"))
    stage = data.pop("stage")
    return stage, ModelRef(**data)


def _flag(item: object) -> QualityFlag:
    """
    {
      責務: [_flag: flag要素をQualityFlagへ復元する]
      処理: [1: キー検証, 2: 校正状態を列挙へ変換, 3: 生成]
      引数: [item: JSON要素]
      戻り値: [QualityFlag]
      エラー: [QualityReportError: 契約違反・未知の校正状態]
    }
    """
    data = dict(_keys(item, set(_FLAG_KEYS), "flag"))
    try:
        data["calibration_state"] = CalibrationState(data["calibration_state"])
    except ValueError as error:
        raise _fail("unknown calibration_state") from error
    return QualityFlag(**data)


_FLAG_KEYS = (
    "code", "stage", "start_sample", "end_sample", "diagnostic", "calibration_state",
    "calibration_id", "heuristic_score", "value", "threshold",
)  # fmt: skip


@dataclass(frozen=True)
class LyricSegmentSignal:
    """
    {
      責務: [LyricSegmentSignal: 歌詞認識区間の合成/実測信号]
      フィールド: [
        start_sample/end_sample: 区間, repeat_ratio: 反復率0..1,
        hallucination_score: 幻覚の目安0..1(確率ではない)
      ]
    }
    """

    start_sample: int
    end_sample: int
    repeat_ratio: float
    hallucination_score: float


@dataclass(frozen=True)
class AlignmentCoverageSignal:
    """
    {
      責務: [AlignmentCoverageSignal: 整列が音声区間をどれだけ覆うか]
      フィールド: [start_sample/end_sample: 区間, coverage: 被覆率0..1]
    }
    """

    start_sample: int
    end_sample: int
    coverage: float


@dataclass(frozen=True)
class BoundarySignal:
    """
    {
      責務: [BoundarySignal: 隣接要素の境界の重なり]
      フィールド: [
        stage: 対象段, start_sample/end_sample: 重なり区間,
        overlap_samples: 重なりsample数
      ]
    }
    """

    stage: str
    start_sample: int
    end_sample: int
    overlap_samples: int


@dataclass(frozen=True)
class IntegrityCheck:
    """
    {
      責務: [IntegrityCheck: 上流の必須契約検査の結果(失敗のみがblockになる)]
      フィールド: [stage: 対象段, check: 検査名, passed: 合否, start_sample/end_sample: 範囲]
    }
    """

    stage: str
    check: str
    passed: bool
    start_sample: int
    end_sample: int


def assess_signals(
    options: QualityOptions,
    *,
    lyrics: tuple[LyricSegmentSignal, ...] = (),
    coverage: tuple[AlignmentCoverageSignal, ...] = (),
    boundaries: tuple[BoundarySignal, ...] = (),
    integrity: tuple[IntegrityCheck, ...] = (),
) -> tuple[QualityFlag, ...]:
    """
    {
      責務: [assess_signals: 信号を閾値で判定しflagを正規順で返す純関数]
      処理: [
        1: 歌詞区間のrepeat_ratio/hallucination_scoreを閾値と比較,
        2: alignment被覆率を閾値と比較,
        3: 境界の重なりを上限と比較,
        4: 失敗した必須検査をhard_integrityにする,
        5: 正規ソートして返す
      ]
      引数: [options: 閾値, lyrics/coverage/boundaries/integrity: 各信号]
      戻り値: [flagのタプル]
      エラー: [QualityReportError: 信号の範囲・値が不正]
    }
    """
    diag = CalibrationState.DIAGNOSTIC
    flags: list[QualityFlag] = []
    for seg in lyrics:
        _unit(seg.repeat_ratio, "repeat_ratio")
        _unit(seg.hallucination_score, "hallucination_score")
        if seg.repeat_ratio >= options.repeat_ratio_min:
            flags.append(
                QualityFlag(
                    "whisper_repeat", "lyrics", seg.start_sample, seg.end_sample,
                    "repeat_ratio", diag, None, seg.repeat_ratio, seg.repeat_ratio,
                    options.repeat_ratio_min,
                )
            )  # fmt: skip
        if seg.hallucination_score >= options.hallucination_score_min:
            flags.append(
                QualityFlag(
                    "whisper_hallucination", "lyrics", seg.start_sample, seg.end_sample,
                    "hallucination_score", diag, None, seg.hallucination_score,
                    seg.hallucination_score, options.hallucination_score_min,
                )
            )  # fmt: skip
    for item in coverage:
        _unit(item.coverage, "coverage")
        if item.coverage < options.alignment_coverage_min:
            flags.append(
                QualityFlag(
                    "alignment_low_coverage", "alignment", item.start_sample, item.end_sample,
                    "alignment_coverage", diag, None, 1 - item.coverage, item.coverage,
                    options.alignment_coverage_min,
                )
            )  # fmt: skip
    for edge in boundaries:
        if not _is_int(edge.overlap_samples) or edge.overlap_samples < 0:
            raise _fail("overlap_samples must be a non-negative integer")
        if edge.overlap_samples > options.boundary_overlap_samples_max:
            flags.append(
                QualityFlag(
                    "boundary_inconsistent", edge.stage, edge.start_sample, edge.end_sample,
                    "boundary_overlap_samples", diag, None, None, float(edge.overlap_samples),
                    float(options.boundary_overlap_samples_max),
                )
            )  # fmt: skip
    for check in integrity:
        if not isinstance(check.passed, bool):
            raise _fail("passed must be a bool")
        if not check.passed:
            flags.append(
                QualityFlag(
                    INTEGRITY_CODE, check.stage, check.start_sample, check.end_sample,
                    check.check, CalibrationState.HARD_INTEGRITY,
                )
            )  # fmt: skip
    return tuple(sorted(flags, key=QualityFlag.sort_key))


def _unit(value: object, label: str) -> None:
    """
    {
      責務: [_unit: 0..1の有限数か検証する]
      処理: [1: 数値性, 2: 範囲]
      引数: [value: 対象, label: エラー表示名]
      戻り値: []
      エラー: [QualityReportError: 範囲外]
    }
    """
    if not _is_num(value) or not 0 <= value <= 1:
        raise _fail(f"{label} must be within 0..1")


def build_quality_report(
    *,
    options: QualityOptions,
    assessor_version: str,
    inputs: tuple[InputRef, ...],
    stage_models: dict[str, ModelRef],
    flags: tuple[QualityFlag, ...],
    attempt: Attempt | None = None,
) -> QualityReport:
    """
    {
      責務: [build_quality_report: 入力・段モデル・flagから正規順のレポートを組む純関数]
      処理: [
        1: assessor(閾値ヒューリスティック、重みなし)をoptions hashで作る,
        2: inputsを名前順, stage_modelsを段順, flagsを正規順に並べる,
        3: 検証付きでQualityReportを返す
      ]
      引数: [
        options: 閾値, assessor_version: 評価器版, inputs: 入力参照,
        stage_models: 段名→ModelRef, flags: 指摘, attempt: 出典(省略時は初回)
      ]
      戻り値: [QualityReport]
      エラー: [QualityReportError: 契約違反]
    }
    """
    assessor = ModelRef("open_karaoke.heuristic_quality", assessor_version, None, options.sha256())
    unknown = [stage for stage in stage_models if stage not in STAGES]
    if unknown:
        raise _fail(f"unknown stage in stage_models: {unknown[0]}")
    return QualityReport(
        assessor=assessor,
        options=options,
        attempt=attempt or Attempt(),
        inputs=tuple(sorted(inputs, key=lambda item: item.artifact)),
        stage_models=tuple((s, stage_models[s]) for s in STAGES if s in stage_models),
        flags=tuple(sorted(flags, key=QualityFlag.sort_key)),
    )
