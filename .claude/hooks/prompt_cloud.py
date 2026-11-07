"""クラウド用UserPromptSubmitフック: 単独の「次」を共通の読み替え指示に展開する（個人情報・E:依存なし）。"""

import json
import sys

NEXT_WORDS = {"次", "つぎ", "次へ", "next", "n"}
NEXT_TEXT = (
    "[自動読み替え] ユーザーの「次」は次の指示を意味する: 現在の目標・Issue・設計書に沿って、設計または実装を"
    "区切りのよい所(関連テスト通過・設計書更新済み・コミット可能な単位)まで確認なしで一気に進める。"
    "迷う時は進める。完了後は結果と次の候補を1回だけ短く報告する。"
    "破壊的操作・課金・秘密・必須レビュー/CIの迂回は従来どおり確認する。"
)


# {"責務":{"main":"「次」だけの入力に読み替え指示を添える"},
#  "処理":["入力JSONを読む","promptが「次」類なら追加文脈を出力する"],
#  "引数":[],"戻り値":[],"副作用":{"stdout":"追加文脈のJSON"}}
def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return
    if str(data.get("prompt", "")).strip().lower() in NEXT_WORDS:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": NEXT_TEXT,
            }
        }
        print(json.dumps(out, ensure_ascii=False))


main()
