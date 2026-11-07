"""クラウド環境用の軽量PreToolUseフック（個人情報・Eドライブ依存なし）。
  1) 前景の5秒超sleepを拒否（待機ごとに文脈全体を再読込するため）
  2) 環境変数 CLOUD_GUARD_BLOCK_TEXT（カンマ区切り）の文字列を外部送信系の入力に含めない（任意）
違反は終了コード2とstderrで拒否する。例外・解析失敗時は許可する。
"""
import json
import os
import re
import sys


# {"責務":{"main":"標準入力のツール呼び出しを検査し、違反なら拒否する"},
#  "処理":["入力JSONを読む","sleep待ちを検査","任意の遮断文字列を外部送信系で検査"],
#  "引数":[],"戻り値":[],"副作用":{"exit":"違反時は終了コード2"}}
def main():
    d = json.load(sys.stdin)
    tool = d.get("tool_name", "")
    inp = d.get("tool_input", {}) or {}
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    if tool in ("Bash", "PowerShell"):
        waits = [int(x) for x in re.findall(r"(?:Start-Sleep(?:\s+-(?:Seconds|s))?|\bsleep)\s+(\d+)", cmd, re.I)]
        if waits and max(waits) > 5 and not inp.get("run_in_background"):
            sys.stderr.write("[guard] 5秒超のsleep待ちは禁止です。長い処理は run_in_background=true で実行し、終了通知を待ってください。\n")
            sys.exit(2)
    block = [t for t in os.environ.get("CLOUD_GUARD_BLOCK_TEXT", "").split(",") if t.strip()]
    if block:
        text = json.dumps(inp, ensure_ascii=False)
        external = tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__") or (
            tool in ("Bash", "PowerShell") and re.search(r"curl|wget|https?://|\bgh\s|git\s+push", cmd, re.I))
        if external and any(t.strip().lower() in text.lower() for t in block):
            sys.stderr.write("[guard] 個人情報を含む外部送信の可能性があるため拒否しました。\n")
            sys.exit(2)


try:
    main()
except SystemExit:
    raise
except Exception:
    pass
