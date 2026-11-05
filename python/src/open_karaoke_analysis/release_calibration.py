"""Versioned threshold ledger: the only way a release threshold moves from null to a value."""

import argparse
import json
import math
import re
from pathlib import Path

from .release_evaluation import _KEY, _within, validate_report, verdict

_HEX64 = re.compile(r"[0-9a-f]{64}")
_LEDGER_KEYS = {"format_version", "ledger_version", "thresholds", "history"}
_ENTRY_KEYS = {"direction", "value"}
_HISTORY_KEYS = {
    "version",
    "key",
    "old",
    "new",
    "tuning_manifest_hash",
    "holdout_manifest_hash",
    "reason",
    "approved_by",
}


def _number(value: object) -> bool:
    """
    {
      責務: [_number: 有限の数値(boolを除く)か判定する]
      処理: [1: bool除外, 2: 有限性を確認する]
      引数: [value: 判定対象]
      戻り値: [有限の数値ならTrue]
    }
    """
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_ledger(ledger: dict) -> None:
    """
    {
      責務: [validate_ledger: 閾値台帳が校正規則を満たすか検証する]
      処理: [
        1: キー・形式を検証する,
        2: historyの版が1から連番で、各項目のoldが直前のnewと一致することを確認する,
        3: 調整用とholdoutのmanifest hashが異なり、理由と承認者があることを確認する,
        4: 台帳の現在値がhistoryを再生した結果と一致し、ledger_versionが履歴数と一致する
      ]
      引数: [ledger: thresholds.jsonの内容]
      戻り値: []
      エラー: [ValueError: 規則違反(historyの無い閾値、holdout流用など)]
    }
    """
    if not isinstance(ledger, dict) or set(ledger) != _LEDGER_KEYS:
        raise ValueError("Ledger must have exactly the contract keys")
    if type(ledger["format_version"]) is not int or ledger["format_version"] != 1:
        raise ValueError("Unsupported ledger format_version")
    thresholds, history = ledger["thresholds"], ledger["history"]
    if not isinstance(thresholds, dict) or not isinstance(history, list):
        raise ValueError("thresholds must be an object and history a list")
    for key, entry in thresholds.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ValueError(f"Invalid threshold key: {key}")
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ValueError(f"{key}: entry must have direction and value")
        if entry["direction"] not in ("max", "min"):
            raise ValueError(f"{key}: direction must be max or min")
        if entry["value"] is not None and not _number(entry["value"]):
            raise ValueError(f"{key}: value must be a finite number or null")
    current: dict[str, object] = {key: None for key in thresholds}
    for position, item in enumerate(history, start=1):
        if not isinstance(item, dict) or set(item) != _HISTORY_KEYS:
            raise ValueError("History entry must have exactly the contract keys")
        if type(item["version"]) is not int or item["version"] != position:
            raise ValueError("History versions must be consecutive from 1")
        key = item["key"]
        if not isinstance(key, str) or key not in current:
            raise ValueError(f"History refers to unknown threshold: {key}")
        if item["old"] != current[key]:
            raise ValueError(f"{key}: old does not match the previous value")
        if item["new"] is not None and not _number(item["new"]):
            raise ValueError(f"{key}: new must be a finite number or null")
        for label in ("tuning_manifest_hash", "holdout_manifest_hash"):
            value = item[label]
            if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
                raise ValueError(f"{key}: {label} must be a lowercase SHA-256 digest")
        if item["tuning_manifest_hash"] == item["holdout_manifest_hash"]:
            raise ValueError(f"{key}: holdout must differ from the tuning data")
        for label in ("reason", "approved_by"):
            if not isinstance(item[label], str) or not item[label].strip():
                raise ValueError(f"{key}: {label} must be nonempty")
        current[key] = item["new"]
    if any(thresholds[key]["value"] != current[key] for key in thresholds):
        raise ValueError("Ledger values must equal the result of replaying history")
    if type(ledger["ledger_version"]) is not int or ledger["ledger_version"] != len(history):
        raise ValueError("ledger_version must equal the number of history entries")


def apply_thresholds(report: dict, ledger: dict) -> dict:
    """
    {
      責務: [apply_thresholds: 台帳の閾値を適用しstatusを再判定した新しい報告を返す]
      処理: [
        1: 台帳と報告を検証する,
        2: 結果ごとに台帳の方向が一致し、報告側の閾値が台帳と食い違わないことを確認する,
        3: 台帳値または計測値がnullならunverified、両方あればpass/failを再判定する,
        4: 再検証して返す
      ]
      引数: [report: report.json, ledger: thresholds.json]
      戻り値: [閾値適用後のreport(元は変更しない)]
      エラー: [ValueError: 方向・閾値の不一致]
    }
    """
    validate_ledger(ledger)
    validate_report(report)
    results = []
    for item in report["results"]:
        key = f"{item['area']}/{item['metric']}"
        entry = ledger["thresholds"].get(key)
        row = dict(item)
        if entry is not None:
            if entry["direction"] != item["direction"]:
                raise ValueError(f"{key}: direction differs from the ledger")
            if item["threshold"] is not None and item["threshold"] != entry["value"]:
                raise ValueError(f"{key}: threshold differs from the ledger")
            row["threshold"] = entry["value"]
            if entry["value"] is None or item["value"] is None:
                row["status"] = "unverified"
            else:
                met = _within(item["value"], entry["value"], item["direction"])
                row["status"] = "pass" if met else "fail"
        results.append(row)
    applied = {**report, "results": results}
    validate_report(applied)
    return applied


def main() -> int:
    """
    {
      責務: [main: 台帳を検証し、報告があれば閾値適用後の判定を出力する]
      処理: [1: 台帳を検証, 2: 報告があれば適用してverdictを計算する]
      引数: []
      戻り値: [0: 台帳が正当(報告指定時はリリース可), 1: リリース不可。入力不正は2で終了]
    }
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("report", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
        validate_ledger(ledger)
        if args.report is None:
            print(json.dumps({"ledger_version": ledger["ledger_version"]}, sort_keys=True))
            return 0
        report = json.loads(args.report.read_text(encoding="utf-8"))
        result = verdict(apply_thresholds(report, ledger))
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["releasable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
