"""Audit that a shipped FFmpeg bundle matches its inventory; mirrors core/src/ffmpeg_bundle.rs."""

import argparse
import hashlib
import json
import re
from pathlib import Path

_HEX64 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN = ("--enable-gpl", "--enable-nonfree")
_MAX_TEXT_BYTES = 256 * 1024
_RULE_THRESHOLD = 1.0


def sha256_file(path: Path) -> str:
    """
    {
      責務: [sha256_file: ファイルのSHA-256を小文字16進で返す]
      処理: [1: 64KiBずつ読み込み, 2: ハッシュを更新する]
      引数: [path: 対象ファイル]
      戻り値: [小文字64桁のSHA-256]
    }
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    """
    {
      責務: [_read_text: 上限付きでUTF-8テキストを読む]
      処理: [1: 上限超過を拒否, 2: UTF-8として復号する]
      引数: [path: 対象ファイル]
      戻り値: [本文]
      エラー: [OSError/UnicodeError/ValueError]
    }
    """
    if path.stat().st_size > _MAX_TEXT_BYTES:
        raise ValueError(f"{path.name} is too large")
    return path.read_text(encoding="utf-8")


def _find_binary(bundle: Path) -> Path | None:
    """
    {
      責務: [_find_binary: 同梱ディレクトリのffmpeg実体を探す]
      処理: [1: ffmpeg または ffmpeg.exe を順に確認する]
      引数: [bundle: tools/ffmpeg ディレクトリ]
      戻り値: [実体のパス。無ければNone]
    }
    """
    for name in ("ffmpeg", "ffmpeg.exe"):
        if (bundle / name).is_file():
            return bundle / name
    return None


def audit_problems(bundle: Path, manifest: dict, inventory: dict | None) -> list[str]:
    """
    {
      責務: [audit_problems: 成果物hashとinventory/manifest/NOTICEの対応を検査し、不一致を列挙する]
      処理: [
        1: ffmpeg実体・buildconf.txt・NOTICE.txtの存在を確認する,
        2: buildconfにGPL/nonfreeが無いことを確認する,
        3: manifestのffmpeg台帳(LGPL系)のsha256が実体と一致することを確認する,
        4: inventoryがあれば実体hashとbuildconf hashの一致を確認する,
        5: NOTICEが台帳の版・sha256・ライセンスを含むことを確認する
      ]
      引数: [bundle: tools/ffmpeg, manifest: runtime-manifest, inventory: ffmpeg-inventory か None]
      戻り値: [不一致の説明の一覧(空なら全て対応)]
      エラー: [FileNotFoundError: 必須ファイル欠落(未検証として扱う)]
    }
    """
    binary = _find_binary(bundle)
    if binary is None:
        raise FileNotFoundError(f"ffmpeg binary in {bundle}")
    conf_path, notice_path = bundle / "buildconf.txt", bundle / "NOTICE.txt"
    for path in (conf_path, notice_path):
        if not path.is_file():
            raise FileNotFoundError(str(path))
    problems: list[str] = []
    lowered = _read_text(conf_path).lower()
    problems += [f"buildconf contains {flag}" for flag in _FORBIDDEN if flag in lowered]
    components = manifest.get("components") if isinstance(manifest, dict) else None
    entries = [c for c in components or [] if isinstance(c, dict) and c.get("name") == "ffmpeg"]
    if len(entries) != 1:
        problems.append("runtime manifest must have exactly one ffmpeg component")
        return problems
    entry = entries[0]
    actual = sha256_file(binary)
    sha, version, license_name = entry.get("sha256"), entry.get("version"), entry.get("license")
    if not isinstance(sha, str) or _HEX64.fullmatch(sha) is None:
        problems.append("manifest sha256 must be lowercase 64-hex")
    elif sha != actual:
        problems.append("ffmpeg sha256 differs from manifest")
    if not isinstance(license_name, str) or not license_name.upper().startswith("LGPL"):
        problems.append("manifest license is not LGPL")
    if inventory is not None:
        if inventory.get("sha256") != actual:
            problems.append("ffmpeg sha256 differs from inventory")
        if inventory.get("buildconf_sha256") != sha256_file(conf_path):
            problems.append("buildconf sha256 differs from inventory")
    notice = _read_text(notice_path)
    for label, needle in (("version", version), ("sha256", sha), ("license", license_name)):
        if not isinstance(needle, str) or not needle or needle not in notice:
            problems.append(f"NOTICE does not mention manifest {label}")
    return problems


def distribution_result(
    bundle: Path, manifest: dict, inventory: dict | None, *, evidence_ref: str
) -> dict:
    """
    {
      責務: [distribution_result: 監査結果をdistribution/license_inventory_match の結果行へ変換する]
      処理: [
        1: 必須ファイルが無ければunverified(値なし),
        2: inventoryが無ければ、台帳全体の対応を確認できないためunverified,
        3: 不一致があればfail(0.0), 全一致ならpass(1.0)
      ]
      引数: [bundle/manifest/inventory: audit_problemsと同じ, evidence_ref: 証拠参照]
      戻り値: [results[]の1要素。閾値1.0は全件一致を要求する規則であり、実データ校正値ではない]
    }
    """
    row = {
        "area": "distribution",
        "metric": "license_inventory_match",
        "value": None,
        "threshold": _RULE_THRESHOLD,
        "direction": "min",
        "status": "unverified",
        "evidence_ref": evidence_ref,
    }
    try:
        problems = audit_problems(bundle, manifest, inventory)
    except (OSError, UnicodeError, ValueError):
        return row
    if inventory is None:
        return row
    row["value"] = 0.0 if problems else 1.0
    row["status"] = "fail" if problems else "pass"
    return row


def main() -> int:
    """
    {
      責務: [main: 同梱FFmpegの対応検査を実行し、結果行をJSONで出力する]
      処理: [1: manifest/inventoryを読む, 2: 検査する, 3: 結果行と不一致を表示する]
      引数: []
      戻り値: [0: 全て対応, 1: fail/unverified。入力不正は2で終了]
    }
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--evidence-ref", required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(_read_text(args.manifest))
        inventory = json.loads(_read_text(args.inventory)) if args.inventory else None
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    row = distribution_result(args.bundle, manifest, inventory, evidence_ref=args.evidence_ref)
    try:
        problems = audit_problems(args.bundle, manifest, inventory)
    except (OSError, UnicodeError, ValueError) as error:
        problems = [str(error)]
    print(json.dumps({"result": row, "problems": problems}, ensure_ascii=False, sort_keys=True))
    return 0 if row["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
