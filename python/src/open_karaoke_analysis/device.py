"""推論デバイスの解決とOOM（VRAM不足）時の段階的フォールバック。

GPU（cuda）を最大限使い、使えない環境だけCPUへ落とす。重いtorchは関数内で遅延importする。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def resolve_device(requested: str) -> str:
    """
    {
      責務: [resolve_device: autoを実際に使うデバイス名へ解決する]
      処理: [
        1: cpu/cudaの明示指定はそのまま返す,
        2: autoならtorchを遅延importしてcuda可否を見る,
        3: torch不在・読込失敗はcpu
      ]
      引数: [requested: auto/cpu/cuda]
      戻り値: [cpu または cuda]
      補足: [
        明示cudaの可否検査は各バックエンドのprepareが行い、利用不可ならdevice_unavailableにする
      ]
    }
    """
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except (ImportError, OSError, AttributeError):
        return "cpu"


def is_oom(error: BaseException) -> bool:
    """
    {
      責務: [is_oom: 例外がCUDAのメモリ不足かを判定する]
      処理: [1: 型名OutOfMemoryErrorを見る, 2: RuntimeErrorなら文言のout of memoryを見る]
      引数: [error: 捕捉した例外]
      戻り値: [OOMならTrue]
      補足: [型名で見るためtorch未導入でも動く]
    }
    """
    if type(error).__name__ == "OutOfMemoryError":
        return True
    return isinstance(error, RuntimeError) and "out of memory" in str(error).lower()


def release_cuda_cache() -> None:
    """
    {
      責務: [release_cuda_cache: OOM後にCUDAの未使用キャッシュを解放する]
      処理: [1: torchを遅延import, 2: cuda可ならempty_cache]
      引数: []
      戻り値: []
      副作用: [VRAMキャッシュの解放。torch不在・cuda不可なら何もしない]
    }
    """
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, OSError, AttributeError, RuntimeError):
        pass


def run_with_oom_fallback(
    stages: list[tuple[str, Callable[[], None] | None]],
    run: Callable[[str], T],
) -> T:
    """
    {
      責務: [run_with_oom_fallback: OOMのときだけ次の段階（縮小→fp32→CPU）へ進めて推論する]
      処理: [
        1: 段階の切替関数を呼ぶ,
        2: runを実行して成功なら返す,
        3: OOMならキャッシュ解放して次段階,
        4: OOM以外は即再送出,
        5: 全段階OOMなら最後の例外
      ]
      引数: [
        stages: (段階名, 切替関数またはNone)の列。先頭が通常設定,
        run: 段階名を受けて推論する関数
      ]
      戻り値: [runの戻り値]
      副作用: [切替関数による状態変更とrelease_cuda_cache]
      エラー: [OOM以外の例外はそのまま, 全段階OOMなら最後のOOM例外]
    }
    """
    last: BaseException | None = None
    for name, switch in stages:
        try:
            if switch is not None:
                switch()
            return run(name)
        except Exception as error:  # noqa: BLE001 - OOM以外は直ちに再送出する
            if not is_oom(error):
                raise
            last = error
            release_cuda_cache()
    assert last is not None
    raise last


def autocast_context(device: str, half: bool) -> Any:
    """
    {
      責務: [autocast_context: cuda時だけfp16の自動混合精度コンテキストを返す]
      処理: [1: cudaかつhalfならtorch.autocast(cuda, float16), 2: それ以外はnullcontext]
      引数: [device: 使用デバイス, half: 半精度を使うか]
      戻り値: [with句で使えるコンテキスト]
      補足: [GTX1080はbf16不可のためfp16固定]
    }
    """
    if device == "cuda" and half:
        import torch

        return torch.autocast("cuda", dtype=torch.float16)
    return contextlib.nullcontext()
