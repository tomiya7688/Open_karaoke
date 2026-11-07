"""クラウド環境用の軽量PreToolUseフック（個人情報・Eドライブ依存なし）。

1. 前景の5秒超sleepを拒否する（待機ごとに文脈全体を再読込するため）。
2. 環境変数 CLOUD_GUARD_BLOCK_TEXT（カンマ区切り）の文字列を外部送信系の入力に含めない（任意）。

違反は終了コード2とstderrで拒否する。解析できない入力は許可し、理由をstderrへ出す。
"""

import json
import os
import re
import sys

SLEEP_RE = re.compile(r"(?:Start-Sleep(?:\s+-(?:Seconds|s))?|\bsleep)\s+(\d+)", re.I)
NET_RE = re.compile(r"curl|wget|https?://|\bgh\s|git\s+push", re.I)


# {"責務":{"deny":"理由をstderrへ出して終了コード2で拒否する"},
#  "処理":["理由を出力する","終了コード2で終了する"],
#  "引数":{"msg":"拒否理由"},
#  "戻り値":[],
#  "副作用":{"stderr":"理由を出力","exit":"終了コード2"}}
def deny(msg):
    sys.stderr.write("[guard] " + msg + "\n")
    sys.exit(2)


# {"責務":{"is_external":"外部送信しうるツール呼び出しかを判定する"},
#  "処理":["Web系・MCP系は外部とみなす","シェルは通信を示す語を含むかで判定する"],
#  "引数":{"tool":"ツール名","cmd":"シェルコマンド文字列"},
#  "戻り値":{"external":"外部送信しうるならTrue"}}
def is_external(tool, cmd):
    if tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
        return True
    return tool in ("Bash", "PowerShell") and bool(NET_RE.search(cmd))


# {"責務":{"main":"標準入力のツール呼び出しを検査し、違反なら拒否する"},
#  "処理":["入力JSONを読む","sleep待ちを検査する","任意の遮断文字列を外部送信系で検査する"],
#  "引数":[],
#  "戻り値":[],
#  "副作用":{"exit":"違反時は終了コード2"},
#  "エラー":{"JSON解析失敗":"許可して理由をstderrへ出す"}}
def main():
    try:
        data = json.load(sys.stdin)
    except ValueError as exc:
        sys.stderr.write("[guard] 入力を解析できないため検査を省略: %s\n" % exc)
        return
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    if tool in ("Bash", "PowerShell"):
        waits = [int(x) for x in SLEEP_RE.findall(cmd)]
        if waits and max(waits) > 5 and not inp.get("run_in_background"):
            deny(
                "5秒超のsleep待ちは禁止です。長い処理は run_in_background=true で実行し、終了通知を待ってください。"
            )
    block = [
        t.strip().lower()
        for t in os.environ.get("CLOUD_GUARD_BLOCK_TEXT", "").split(",")
        if t.strip()
    ]
    if block and is_external(tool, cmd):
        text = json.dumps(inp, ensure_ascii=False).lower()
        if any(t in text for t in block):
            deny("個人情報を含む外部送信の可能性があるため拒否しました。")


main()
