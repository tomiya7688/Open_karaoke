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
# 数値部の後ろに認める接尾辞の文法(PEP 440 の表記の一部)。1語は「種類 + 数字(省略可) + 区切りか末尾」。
# これに合わない接尾辞は比較不能として拒否する(セキュリティ判定なので、未知の表記を正式版として通さない)。
SUFFIX_TOKEN = re.compile(
    r"(?P<kind>alpha|beta|preview|pre|post|rev|rc|dev|a|b|c|r)\d*(?:[.\-_]|$)",
    re.IGNORECASE,
)
# 先頭の語がこれなら、同じ数値の正式版より小さい版として扱う。
PRERELEASE_KINDS = frozenset(
    {"a", "b", "c", "rc", "alpha", "beta", "pre", "preview", "dev"}
)
WIDTH = 4  # 比較用に数値部を揃える桁数。"26.2" と "26.2.0" を同じ版として扱う


def parse_version(text: str) -> tuple[int, ...]:
    """
    {
      責務: [parse_version: バージョン文字列を大小比較できる固定長の組へ変換する]
      処理: [
        1: 先頭の数字とドットの並びを取り出し、WIDTH桁まで0で埋める,
        2: ローカル版識別子("+"以降)を取り除く,
        3: 残りをSUFFIX_TOKENの列として読み、合わない接尾辞は拒否する,
        4: 先頭の語が事前版・開発版なら末尾に0、それ以外(接尾辞なし・post)は1を付ける
      ]
      引数: [text: pip・certifiなどのバージョン文字列]
      戻り値: [比較用の組。例: "26.2.2" -> (26, 2, 2, 0, 1)、"26.2.2rc1" -> (26, 2, 2, 0, 0)]
      エラー: [ValueError: 先頭が数字で始まらない、数値部がWIDTH桁を超える、または未知の接尾辞]
    }
    """
    match = re.match(r"\d+(?:\.\d+)*", text.strip())
    if match is None:
        raise ValueError(f"Unrecognized version: {text!r}")
    numbers = [int(part) for part in match.group().split(".")]
    if len(numbers) > WIDTH:
        raise ValueError(f"Unsupported version shape: {text!r}")
    # "26.2.2+build1" のようなローカル版識別子は事前版の判定に使わない。
    public_rest = text.strip()[match.end() :].split("+", 1)[0].lstrip(".-_")
    first_kind = None
    position = 0
    while position < len(public_rest):
        token = SUFFIX_TOKEN.match(public_rest, position)
        if token is None:
            raise ValueError(f"Unsupported version suffix: {text!r}")
        first_kind = first_kind or token.group("kind").lower()
        position = token.end()
    final = 0 if first_kind in PRERELEASE_KINDS else 1
    return (*numbers, *([0] * (WIDTH - len(numbers))), final)


def find_problems(installed: Mapping[str, str | None]) -> list[str]:
    """
    {
      責務: [find_problems: 導入済みの版が最小版を満たすか判定する]
      処理: [
        1: pipとcertifiが導入済みで最小版以上か確認する,
        2: uvとsigstoreは導入されている場合だけ最小版以上か確認する,
        3: 比較できない版文字列は例外にせず問題として報告する
      ]
      引数: [installed: パッケージ名から版(未導入ならNone)への対応]
      戻り値: [問題の説明の一覧。空なら全て適合]
    }
    """
    problems = []
    for name, minimum in {**REQUIRED_MINIMUMS, **OPTIONAL_MINIMUMS}.items():
        version = installed.get(name)
        if version is None:
            if name in REQUIRED_MINIMUMS:
                problems.append(f"{name} is not installed (need >= {_dotted(minimum)})")
            continue
        try:
            too_old = parse_version(version) < parse_version(_dotted(minimum))
        except ValueError:
            problems.append(f"{name} version {version!r} cannot be compared")
            continue
        if too_old:
            problems.append(f"{name} {version} is older than {_dotted(minimum)}")
    return problems


def find_bundle_problems(bundle: Path) -> list[str]:
    """
    {
      責務: [find_bundle_problems: 実際に参照するCA bundleが読める実体か判定する]
      処理: [1: 通常ファイルとして存在するか確認する, 2: 空でないか確認する, 3: 参照できない場合は問題として返す]
      引数: [bundle: certifi.where()が返すCA bundleのパス]
      戻り値: [問題の説明の一覧。空なら実体がある]
    }
    """
    try:
        if not bundle.is_file():
            return [f"certifi CA bundle does not exist: {bundle}"]
        if bundle.stat().st_size == 0:
            return [f"certifi CA bundle is empty: {bundle}"]
    except OSError as error:
        return [f"certifi CA bundle cannot be accessed: {bundle} ({error})"]
    return []


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
        4: 最小版を満たさない、またはCA bundleの実体が無い・空なら終了コード1にする
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
    bundle_problems: list[str] = []
    if installed["certifi"] is not None:
        try:
            import certifi

            bundle = Path(certifi.where())
        except (
            ImportError,
            OSError,
        ) as error:  # 壊れた導入でも例外ではなく問題として報告する
            bundle_problems = [f"certifi cannot be imported: {error!r}"]
        else:
            print(f"certifi CA bundle: {bundle} (exists={bundle.is_file()})")
            bundle_problems = find_bundle_problems(bundle)
    paths = ssl.get_default_verify_paths()
    print(f"ssl default cafile: {paths.cafile}  capath: {paths.capath}")
    for variable in TRUST_OVERRIDE_VARIABLES:
        if os.environ.get(variable):
            print(
                f"WARNING: {variable} overrides the trust store: {os.environ[variable]}"
            )
    problems = [*find_problems(installed), *bundle_problems]
    for problem in problems:
        print(f"ERROR: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
