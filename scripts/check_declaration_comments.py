#!/usr/bin/env python3
"""{
  責務: [PR差分に新しく現れたソース宣言だけのJSON-likeコメント契約を確認する]
  フィールド: []
  補足: [既存コード全体のコメント準拠を保証するツールではない]
}"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:(?P<type>struct|enum|trait)\s+\w+|(?:async\s+)?fn\s+\w+\s*\()"
)
CS_ACCESS = re.compile(r"^\s*(?:public|internal|protected|private)\s+")
CS_TYPE = re.compile(r"\b(?:class|record|interface|struct|enum)\s+\w+")
CS_METHOD = re.compile(r"\b[A-Za-z_]\w*\s*\(")
REQUIRED_TYPE = ("責務:", "フィールド:")
REQUIRED_CALL = ("責務:", "処理:", "引数:", "戻り値:")


def parse_added_lines(diff: str) -> dict[str, set[int]]:
    """{
      責務: [unified diffの追加行だけを変更後ファイルの行番号へ対応付ける]
      処理: [1: 新ファイルpathを読む, 2: hunkの追加側開始行を読む, 3: 追加行だけ記録する]
      引数: [diff: git diff --unified=0が出力する文字列]
      戻り値: [ファイル相対pathから追加行番号集合への辞書]
    }"""
    found: dict[str, set[int]] = {}
    path: str | None = None
    line_no: int | None = None
    for row in diff.splitlines():
        if row.startswith("diff --git "):
            path, line_no = None, None
        elif row.startswith("+++ b/"):
            path = row[len("+++ b/") :]
            found.setdefault(path, set())
        elif row.startswith("+++ /dev/null"):
            path, line_no = None, None
        elif row.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?\s+@@", row)
            line_no = int(match.group(1)) if match else None
        elif path is not None and line_no is not None and row.startswith("+"):
            found[path].add(line_no)
            line_no += 1
        elif path is not None and line_no is not None and row.startswith(" "):
            line_no += 1
    return found


def production_language(path: str) -> str | None:
    """{
      責務: [本番コードの宣言コメントチェック対象言語を決める]
      処理: [1: sourceディレクトリを確認する, 2: 拡張子を照合する]
      引数: [path: リポジトリ相対path]
      戻り値: [py/rs/cs。テスト・生成物・その他はNone]
    }"""
    if path.startswith("core/src/") and path.endswith(".rs"):
        return "rs"
    if path.startswith("python/src/open_karaoke_analysis/") and path.endswith(".py"):
        return "py"
    if (
        path.startswith(("gui/OpenKaraoke.Client/", "gui/OpenKaraoke.Gui/"))
        and path.endswith(".cs")
    ):
        return "cs"
    return None


def preceding_docs(lines: list[str], index: int) -> str:
    """{
      責務: [Rust/C#の宣言に直結する///ドキュメントコメントだけを収集する]
      処理: [1: 属性と空白を遡る, 2: ///の連続ブロックを収集する, 3: 他の実コードで打ち切る]
      引数: [lines: ソース各行, index: 宣言行の0始まり位置]
      戻り値: [宣言に結びつくドキュメントコメント文字列]
    }"""
    doc: list[str] = []
    for line in reversed(lines[max(0, index - 40) : index]):
        item = line.strip()
        if item.startswith("///"):
            doc.append(item)
        elif not doc and (item == "" or item.startswith(("#[", "[", "]"))):
            continue
        else:
            break
    return "\n".join(reversed(doc))


def check_python(source: str, added: set[int]) -> list[tuple[int, str, tuple[str, ...]]]:
    """{
      責務: [新規/署名変更されたPython宣言のdocstring必須項目を検査する]
      処理: [1: ASTを読む, 2: diff追加行と宣言の署名を照合する, 3: docstringの必要項目を確認する]
      引数: [source: Pythonソース, added: 追加行番号]
      戻り値: [違反の行番号・宣言名・欠落フィールド一覧]
    }"""
    issues: list[tuple[int, str, tuple[str, ...]]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        signature_end = body[0].lineno if body else node.lineno + 1
        if not any(node.lineno <= i < signature_end for i in added):
            continue
        text = ast.get_docstring(node, clean=False) or ""
        required = REQUIRED_TYPE if isinstance(node, ast.ClassDef) else REQUIRED_CALL
        missing = tuple(field for field in required if field not in text)
        if missing:
            issues.append((node.lineno, node.name, missing))
    return issues


def check_slash_docs(
    source: str, added: set[int], language: str
) -> list[tuple[int, str, tuple[str, ...]]]:
    """{
      責務: [変更されたRust/C#の型・関数宣言の///コメント必須項目を検査する]
      処理: [1: 変更された行だけ調べる, 2: 宣言種別を判定する, 3: 直前の///を確認する]
      引数: [source: ソース, added: 追加行番号, language: rsまたはcs]
      戻り値: [違反の行番号・宣言・欠落フィールド一覧]
      補足: [正規表現の近似判定。複雑な複数行署名やマクロの網羅性は保証しない]
    }"""
    lines = source.splitlines()
    issues: list[tuple[int, str, tuple[str, ...]]] = []
    for line_no in sorted(added):
        if not 1 <= line_no <= len(lines):
            continue
        item = lines[line_no - 1]
        kind: str | None = None
        if language == "rs":
            match = RUST.match(item)
            if match:
                kind = "type" if match.group("type") else "call"
        elif CS_ACCESS.match(item):
            if CS_TYPE.search(item):
                kind = "type"
            elif CS_METHOD.search(item) and not item.strip().startswith("public event "):
                kind = "call"
        if kind is None:
            continue
        doc = preceding_docs(lines, line_no - 1)
        required = REQUIRED_TYPE if kind == "type" else REQUIRED_CALL
        missing = tuple(field for field in required if field not in doc)
        if missing:
            issues.append((line_no, item.strip()[:96], missing))
    return issues


def inspect_changes(base: str, head: str = "HEAD") -> tuple[int, list[str]]:
    """{
      責務: [git差分の本番宣言だけを収集し、コメント契約違反を返す]
      処理: [1: base/head差分を取得する, 2: 対象ソースを読む, 3: 宣言コメント検査を委譲する]
      引数: [base: 比較元git ref, head: 比較先git ref]
      戻り値: [確認した宣言の概数と違反メッセージ]
      エラー: [git差分取得またはPython AST解析に失敗した場合は例外を送出する]
    }"""
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--unified=0", "--diff-filter=ACMR",
         base, head, "--", "core/src", "python/src/open_karaoke_analysis",
         "gui/OpenKaraoke.Client", "gui/OpenKaraoke.Gui"],
        cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout
    rows = parse_added_lines(diff)
    checked = 0
    failures: list[str] = []
    for path, added in sorted(rows.items()):
        lang = production_language(path)
        if lang is None or not added:
            continue
        content = (ROOT / path).read_text(encoding="utf-8")
        errors = (
            check_python(content, added)
            if lang == "py" else check_slash_docs(content, added, lang)
        )
        # Count is a lower bound (only source declaration lines are inspected).
        checked += len(errors)
        for line_no, name, missing in errors:
            failures.append(f"{path}:{line_no}: {name}: 不足 {', '.join(missing)}")
    return checked, failures


def main() -> int:
    """{
      責務: [新規/変更した宣言のコメント不足をCIで失敗として示す]
      処理: [1: 比較元を取得する, 2: 差分を検査する, 3: 結果と対象範囲を出力する]
      引数: []
      戻り値: [終了コード0=検出なし、1=規約違反、2=検証不能]
    }"""
    parser = argparse.ArgumentParser(description="PR差分の宣言コメントを確認")
    parser.add_argument("--base", required=True, help="比較元git commit/ref")
    parser.add_argument("--head", default="HEAD", help="比較先git commit/ref")
    opts = parser.parse_args()
    try:
        checked, errors = inspect_changes(opts.base, opts.head)
    except (OSError, UnicodeError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"UNVERIFIED: 宣言コメント差分検査を実施できません: {error}", file=sys.stderr)
        return 2
    for message in errors:
        print(message, file=sys.stderr)
    print(
        f"宣言コメント差分検査: {'FAIL' if errors else 'PASS'}。"
        "対象: 追加/署名変更された本番ソースの宣言（既存コード全体は未監査）。"
        f" 違反={len(errors)}。"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
