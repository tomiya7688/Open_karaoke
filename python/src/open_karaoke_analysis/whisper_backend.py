"""Pinned local Whisper runtime; no model imports or downloads at service startup."""

import hashlib
import importlib.metadata
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .adapters import AnalysisContext, resolve_artifact
from .artifact_io import hash_file
from .contracts import ServiceError
from .device import run_with_oom_fallback

WHISPER_VERSION = "20250625"
# Digests from openai/whisper's publisher model registry, not arbitrary user URLs.
WEIGHTS = {
    "large-v3": "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
    "large-v3-turbo": "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
    "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
}
MODEL_HOST = "https://openaipublic.azureedge.net/main/whisper/models"
MAX_WEIGHT_BYTES = 4_000_000_000


def sha256_file(path: Path, context: AnalysisContext) -> str:
    return hash_file(path, checkpoint=context.checkpoint, final_checkpoint=True)[0]


def verified_checkpoint(context: AnalysisContext, name: str, allow_download: bool) -> Path:
    """Stream, verify and atomically publish weights; corrupt local files fail closed."""
    digest = WEIGHTS[name]
    path = resolve_artifact(context.root, f"models/whisper/{name}.pt")
    context.checkpoint()
    if path.exists():
        if not path.is_file() or sha256_file(path, context) != digest:
            raise ServiceError("model_integrity", "Whisper checkpoint failed SHA-256 verification")
        return path
    if not allow_download:
        raise ServiceError("model_not_cached", "Pinned Whisper weights are not installed", 503)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    deadline = time.monotonic() + 900
    try:
        context.report(0.02, "downloading_whisper")
        request = urllib.request.Request(
            f"{MODEL_HOST}/{digest}/{name}.pt", headers={"User-Agent": "OpenKaraoke/0.1"}
        )
        with os.fdopen(fd, "wb") as output:
            with urllib.request.urlopen(request, timeout=30) as response:
                if not response.url.startswith("https://"):
                    raise ServiceError("model_download", "Insecure model redirect", 502)
                downloaded = hashlib.sha256()
                size = 0
                while block := response.read(1024 * 1024):
                    context.checkpoint()
                    size += len(block)
                    if size > MAX_WEIGHT_BYTES or time.monotonic() > deadline:
                        raise ServiceError(
                            "model_download", "Model download exceeded its limit", 503
                        )
                    downloaded.update(block)
                    output.write(block)
                if downloaded.hexdigest() != digest:
                    raise ServiceError("model_integrity", "Downloaded Whisper weights are invalid")
            output.flush()
            os.fsync(output.fileno())
        context.checkpoint()
        os.replace(temporary, path)
        return path
    except (urllib.error.URLError, TimeoutError) as error:
        raise ServiceError(
            "model_download", "Could not download Whisper weights", 503, True
        ) from error
    finally:
        Path(temporary).unlink(missing_ok=True)


class WhisperBackend:
    """Replaceable backend: identity, prepare, transcribe, close. Tiny is test-only."""

    def __init__(self, name: str = "large-v3"):
        if name not in WEIGHTS:
            raise ValueError("Unsupported pinned Whisper model")
        self.name = name
        self.model = None
        self.device = "cpu"
        self.half = False

    def identity(self) -> dict:
        versions = {}
        for package in ("openai-whisper", "torch", "numpy", "scipy", "soundfile"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        return {
            "id": f"whisper-{self.name}",
            "version": self.name,
            "backend": "openai-whisper",
            "backend_version": WHISPER_VERSION,
            "weights_sha256": WEIGHTS[self.name],
            "weights_url": f"{MODEL_HOST}/{WEIGHTS[self.name]}/{self.name}.pt",
            "code_license": "MIT",
            "weights_license": "MIT",
            "runtime_versions": versions,
        }

    def prepare(self, context: AnalysisContext, options: dict) -> None:
        identity = self.identity()
        installed = identity["runtime_versions"]["openai-whisper"]
        if installed != WHISPER_VERSION:
            raise ServiceError(
                "dependency_missing",
                f"Install openai-whisper=={WHISPER_VERSION} (lyrics extra)",
                503,
            )
        try:
            import torch
            from whisper.model import ModelDimensions, Whisper
            from whisper.tokenizer import LANGUAGES
        except (ImportError, OSError) as error:
            raise ServiceError(
                "dependency_missing", "Whisper runtime could not be loaded", 503
            ) from error
        if options["language"] is not None and options["language"] not in LANGUAGES:
            raise ServiceError("invalid_language", "Unsupported Whisper language code")
        if options["device"] == "cuda" and not torch.cuda.is_available():
            raise ServiceError("device_unavailable", "CUDA was requested but is unavailable", 503)
        path = verified_checkpoint(context, self.name, options["allow_model_download"])
        context.report(0.04, "loading_whisper")
        context.checkpoint()
        try:
            # Never fall back to unsafe pickle loading or a different model.
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
            model = Whisper(ModelDimensions(**checkpoint["dims"]))
            model.load_state_dict(checkpoint["model_state_dict"], strict=True)
            del checkpoint
            self.device = options["device"]
            # cudaでは重みを半精度で1回だけ転送する(fp16推論)。CPUはfp32のまま。
            self.model = (model.half() if self.device == "cuda" else model).to(self.device).eval()
            self.half = self.device == "cuda"
        except (RuntimeError, KeyError, TypeError, ValueError) as error:
            raise ServiceError(
                "model_load", "Verified Whisper checkpoint could not be loaded", 503
            ) from error
        context.checkpoint()

    def transcribe(self, audio, options: dict) -> dict:
        """
        {
          責務: [
            transcribe: 16kHz monoの音声を文字起こしし、OOM時はbeam縮小・fp32・CPUへ落とす
          ]
          処理: [
            1: モデル準備を確認,
            2: 現在のデバイス/精度で推論,
            3: cudaでOOMならbeam=1→fp32→CPUの順に再試行,
            4: 失敗をServiceErrorへ変換
          ]
          引数: [audio: 16kHz monoのfloat32配列, options: 検証済みのdevice/language/beam_size]
          戻り値: [Whisperのtranscribe結果dict]
          副作用: [OOM時はモデルを一度だけfp32/CPUへ移し、以降もその状態を保つ]
          エラー: [ServiceError: model_not_ready, inference_failed]
        }
        """
        if self.model is None:
            raise ServiceError("model_not_ready", "Whisper is not ready", 503)
        import torch

        beam = {"value": options["beam_size"]}

        def shrink_beam() -> None:
            """
            {
              責務: [shrink_beam: OOM段階: beamを1へ縮小する]
              処理: [1: beamを1にする]
              引数: [なし]
              戻り値: [None]
            }
            """
            beam["value"] = 1

        def to_fp32() -> None:
            """
            {
              責務: [to_fp32: OOM段階: fp16をやめfp32へ切り替える]
              処理: [1: 半精度フラグを下げる(必要なら重みをfloat化)]
              引数: [なし]
              戻り値: [None]
            }
            """
            self.model = self.model.float()
            self.half = False

        def to_cpu() -> None:
            """
            {
              責務: [to_cpu: OOM段階: モデルをCPU/fp32へ移す]
              処理: [1: モデルをCPUへ転送し状態を更新する]
              引数: [なし]
              戻り値: [None]
            }
            """
            self.model = self.model.float().to("cpu")
            self.device, self.half = "cpu", False

        stages = [("gpu_fp16", None)]
        if self.device == "cuda":
            stages += [("beam1", shrink_beam), ("gpu_fp32", to_fp32), ("cpu_fp32", to_cpu)]

        def run(_stage: str) -> dict:
            """
            {
              責務: [run: 段階ごとの1回分の推論を実行する]
              処理: [1: 入力を対象デバイスへ置いて推論し結果を返す]
              引数: [_stage: 段階名(未使用)]
              戻り値: [推論結果]
            }
            """
            # Array input is already mono/16 kHz: no FFmpeg subprocess or network audio I/O.
            # Keep low-confidence raw hypotheses; the adapter flags them separately.
            with torch.inference_mode():
                return self.model.transcribe(
                    audio,
                    language=options["language"],
                    task="transcribe",
                    temperature=0.0,
                    beam_size=beam["value"],
                    condition_on_previous_text=False,
                    word_timestamps=False,
                    fp16=self.half,
                    verbose=None,
                    no_speech_threshold=None,
                    logprob_threshold=None,
                    compression_ratio_threshold=None,
                )

        try:
            return run_with_oom_fallback(stages, run)
        except (RuntimeError, ValueError) as error:
            raise ServiceError("inference_failed", "Whisper transcription failed", 500) from error

    def close(self) -> None:
        self.model = None
