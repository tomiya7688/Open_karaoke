"""Verify the Python packaging/trust tooling that every Open Karaoke environment must use.

Standard library only: it runs before any project dependency is installed.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import ssl
import sys
from collections.abc import Mapping
from pathlib import Path

# 証明書信頼ストア対応(#66)で要求する最小版。
REQUIRED_MINIMUMS: dict[str, tuple[int, ...]] = {
    "pip": (26, 2, 2),
    "certifi": (2026, 10, 19),
}
# 使っている場合だけ最小版を要求する。未使用なら「未導入」として記録する。
OPTIONAL_MINIMUMS: dict[str, tuple[int, ...]] = {
    "uv": (0, 13, 4),
    "sigstore": (4, 5, 1),
}
TRUST_OVERRIDE_VARIABLES = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
)
PRERELEASE = re.compile(r"(a|b|rc|alpha|beta|dev|pre)\d*", re.IGNORECASE)
WIDTH = 4  # 比較用に数値部を揃える桁数。"26.2" と "26.2.0" を同じ版として扱う


def parse_version(text: str) -> tuple[int, ...]:
    """
    {
      責務: [parse_version: バージョン文字列を大小比較できる固定長の組へ変換する]
      処理: [
        1: 先頭の数字とドットの並びを取り出し、WIDTH桁まで0で埋める,
        2: 末尾に正式版なら1、開発版・事前版なら0を付け、同じ数値の事前版を正式版より小さくする
      ]
      引数: [text: pip・certifiなどのバージョン文字列]
      戻り値: [比較用の組。例: "26.2.2" -> (26, 2, 2, 0, 1)、"26.2.2rc1" -> (26, 2, 2, 0, 0)]
      エラー: [ValueError: 先頭が数字で始まらない、または数値部がWIDTH桁を超える]
    }
    """
    match = re.match(r"\d+(?:\.\d+)*", text.strip())
    if match is None:
        raise ValueError(f"Unrecognized version: {text!r}")
    numbers = [int(part) for part in match.group().split(".")]
    if len(numbers) > WIDTH:
        raise ValueError(f"Unsupported version shape: {text!r}")
    rest = text.strip()[match.end() :].lstrip(".-+_")
    final = 0 if PRERELEASE.match(rest) else 1
    return (*numbers, *([0] * (WIDTH - len(numbers))), final)


def find_problems(installed: Mapping[str, str | None]) -> list[str]:
    """
    {
      責務: [find_problems: 導入済みの版が最小版を満たすか判定する]
      処理: [
        1: pipとcertifiが導入済みで最小版以上か確認する,
        2: uvとsigstoreは導入されている場合だけ最小版以上か確認する
      ]
      引数: [installed: パッケージ名から版(未導入ならNone)への対応]
      戻り値: [問題の説明の一覧。空なら全て適合]
    }
    """
    problems = []
    for name, minimum in REQUIRED_MINIMUMS.items():
        version = installed.get(name)
        if version is None:
            problems.append(f"{name} is not installed (need >= {_dotted(minimum)})")
        elif parse_version(version) < parse_version(_dotted(minimum)):
            problems.append(f"{name} {version} is older than {_dotted(minimum)}")
    for name, minimum in OPTIONAL_MINIMUMS.items():
        version = installed.get(name)
        if version is not None and parse_version(version) < parse_version(
            _dotted(minimum)
        ):
            problems.append(f"{name} {version} is older than {_dotted(minimum)}")
    return problems


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> int:
    """
    {
      責務: [main: 実行中のPython環境の版と、実際に参照する信頼ストアを表示して検査する]
      処理: [
        1: Python・pip・certifi・uv・sigstoreの版を集める,
        2: certifiのCA bundleと標準ライブラリのSSL検証パスを表示する,
        3: 信頼ストアを上書きする環境変数があれば警告する,
        4: 最小版を満たさなければ終了コード1にする
      ]
      引数: []
      戻り値: [終了コード。0なら適合]
    }
    """
    names = [*REQUIRED_MINIMUMS, *OPTIONAL_MINIMUMS]
    installed = {name: _installed_version(name) for name in names}
    print(f"python {sys.version.split()[0]} ({sys.executable})")
    for name in names:
        print(f"{name}: {installed[name] or 'not installed'}")
    if installed["certifi"] is not None:
        import certifi

        bundle = Path(certifi.where())
        print(f"certifi CA bundle: {bundle} (exists={bundle.is_file()})")
    paths = ssl.get_default_verify_paths()
    print(f"ssl default cafile: {paths.cafile}  capath: {paths.capath}")
    for variable in TRUST_OVERRIDE_VARIABLES:
        if os.environ.get(variable):
            print(
                f"WARNING: {variable} overrides the trust store: {os.environ[variable]}"
            )
    problems = find_problems(installed)
    for problem in problems:
        print(f"ERROR: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
