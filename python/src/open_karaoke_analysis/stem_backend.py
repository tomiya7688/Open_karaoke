"""Pinned, lazily loaded UMX-HQ vocals model. No torch.hub code execution."""

import hashlib
import importlib.metadata
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .adapters import AnalysisContext, resolve_artifact
from .artifact_io import hash_file
from .contracts import ServiceError
from .device import autocast_context, run_with_oom_fallback

MODEL_ID = "umxhq-vocals"
WEIGHT_VERSION = "1.0.1"
WEIGHT_FILE = "vocals-b62c91ce.pth"
WEIGHT_SHA256 = "b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083"
WEIGHT_BYTES = 35_637_796
WEIGHT_URL = f"https://zenodo.org/api/records/3370489/files/{WEIGHT_FILE}/content"
MODEL_RATE = 44_100
# OOM時の二分割: 各半分に足す前後文脈(約1秒)と、分割を許す半分の最小長(約2秒)。
SPLIT_CONTEXT = MODEL_RATE
SPLIT_MIN_HALF = 2 * MODEL_RATE


def spectral_state_dict(state: dict) -> dict:
    """Migrate the pinned v1 checkpoint's transform buffers, not learned parameters.

    The publisher checkpoint bundled waveform transforms into OpenUnmix. Version 1.3
    moves these to Separator. Only these three known keys may be removed, and only as
    a complete set. The caller verifies the full checkpoint digest first and strictly
    loads every remaining parameter; unknown keys are never silently discarded.
    """
    legacy = {"sample_rate", "stft.window", "transform.0.window"}
    if legacy.intersection(state) and not legacy.issubset(state):
        raise ServiceError("model_incompatible", "Incomplete legacy transform buffer set", 503)
    return {key: value for key, value in state.items() if key not in legacy}


def file_sha256(path: Path, context: AnalysisContext) -> str:
    return hash_file(path, checkpoint=context.checkpoint, final_checkpoint=True)[0]


def verified_weights(context: AnalysisContext) -> Path:
    """Download only the pinned publisher file; never load an unverified checkpoint."""
    context.checkpoint()
    path = resolve_artifact(context.root, f"models/umxhq/{WEIGHT_FILE}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size != WEIGHT_BYTES or file_sha256(path, context) != WEIGHT_SHA256:
            raise ServiceError(
                "model_integrity", "Cached UMX-HQ weights failed SHA-256 verification"
            )
        return path
    context.report(0.05, "downloading_model")
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    try:
        request = urllib.request.Request(WEIGHT_URL, headers={"User-Agent": "OpenKaraoke/0.1"})
        with os.fdopen(fd, "wb") as stream:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if not response.url.startswith("https://"):
                        raise ServiceError(
                            "model_download", "Insecure model download redirect", 502
                        )
                    digest = hashlib.sha256()
                    size = 0
                    while block := response.read(256 * 1024):
                        context.checkpoint()
                        size += len(block)
                        if size > WEIGHT_BYTES:
                            raise ServiceError("model_integrity", "Unexpected model download size")
                        digest.update(block)
                        stream.write(block)
                if size != WEIGHT_BYTES or digest.hexdigest() != WEIGHT_SHA256:
                    raise ServiceError(
                        "model_integrity", "Downloaded UMX-HQ weights failed verification"
                    )
            except (urllib.error.URLError, TimeoutError) as error:
                raise ServiceError(
                    "model_download", "Could not download pinned UMX-HQ weights", 503, True
                ) from error
            stream.flush()
            os.fsync(stream.fileno())
        context.checkpoint()
        # Unique temporary files allow two service instances to download without collisions.
        os.replace(temporary, path)
        return path
    finally:
        Path(temporary).unlink(missing_ok=True)


class UmxHqBackend:
    """Model-independent pipeline expects identity(), prepare(), and vocals()."""

    def identity(self, device: str) -> dict:
        try:
            packages = {
                name: importlib.metadata.version(name)
                for name in ("openunmix", "torch", "torchaudio", "numpy", "scipy", "soundfile")
            }
        except importlib.metadata.PackageNotFoundError as error:
            raise ServiceError(
                "dependency_missing", "Install the analysis package with the stems extra", 503
            ) from error
        if packages["openunmix"] != "1.3.0":
            raise ServiceError("incompatible_runtime", "This adapter requires openunmix 1.3.0", 503)
        return {
            "id": MODEL_ID,
            "version": WEIGHT_VERSION,
            "weights_sha256": WEIGHT_SHA256,
            "weights_url": WEIGHT_URL,
            "weights_license": "MIT",
            "license_record": "https://zenodo.org/records/3370489",
            "code_license": "MIT",
            "model_sample_rate": MODEL_RATE,
            "device": device,
            "runtime_versions": packages,
        }

    def prepare(self, context: AnalysisContext, device: str, niter: int) -> None:
        try:
            import torch
            from openunmix import umxhq_spec
            from openunmix.model import Separator
        except (ImportError, OSError) as error:
            raise ServiceError(
                "dependency_missing", "The stems runtime could not be loaded", 503
            ) from error
        if device == "cuda" and not torch.cuda.is_available():
            raise ServiceError("device_unavailable", "CUDA was requested but is unavailable", 503)
        path = verified_weights(context)
        context.checkpoint()
        context.report(0.08, "loading_model")
        # No unsafe pickle fallback, no arbitrary classes, no downloaded Python source.
        state = torch.load(path, map_location="cpu", weights_only=True)
        targets = umxhq_spec(targets=["vocals"], pretrained=False, device="cpu")
        targets["vocals"].load_state_dict(spectral_state_dict(state), strict=True)
        # CPUで検証済み重みを組み立ててから1回だけ対象デバイスへ転送する(二重転送しない)。
        self.model = Separator(
            targets,
            residual=True,
            niter=niter,
            sample_rate=MODEL_RATE,
            n_fft=4096,
            n_hop=1024,
            wiener_win_len=300,
        ).to(device)
        self.model.freeze()
        self.device = device
        self.half = device == "cuda"
        self.split = False
        context.checkpoint()

    def vocals(self, samples):
        """
        {
          責務: [
            vocals: 44.1kHzステレオの1チャンクからボーカルを推定し、OOM時は縮小→fp32→CPUへ落とす
          ]
          処理: [
            1: 現在のデバイス/精度で推論,
            2: cudaでOOMならチャンクを前後文脈つきで二分割,
            3: それでもOOMならfp32,
            4: 最後にCPU
          ]
          引数: [samples: float32 frames x stereo channels, 44.1kHz]
          戻り値: [入力と同じ長さのボーカルfloat32配列(frames x 2)]
          副作用: [OOM時はモデルを一度だけ分割/fp32/CPUへ切り替え、以降もその状態を保つ]
          エラー: [OOM以外の例外はそのまま、全段階OOMなら最後のOOM例外]
        }
        """

        def shrink() -> None:
            """
            {
              責務: [shrink: OOM段階: チャンクの二分割推論を有効にする]
              処理: [1: splitを有効にする]
              引数: [なし]
              戻り値: [None]
            }
            """
            self.split = True

        def to_fp32() -> None:
            """
            {
              責務: [to_fp32: OOM段階: fp16をやめfp32へ切り替える]
              処理: [1: 半精度フラグを下げる(必要なら重みをfloat化)]
              引数: [なし]
              戻り値: [None]
            }
            """
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
            self.model = self.model.to("cpu")
            self.device, self.half, self.split = "cpu", False, False

        stages = [("gpu_fp16", None)]
        if self.device == "cuda":
            stages += [("split", shrink), ("gpu_fp32", to_fp32), ("cpu_fp32", to_cpu)]
        return run_with_oom_fallback(stages, lambda _stage: self._separate(samples))

    def _separate(self, samples):
        """
        {
          責務: [_separate: 分割モードなら前後文脈つきで二分割して推論し、結合する]
          処理: [
            1: 分割モードかつ十分長ければ中点で二分,
            2: 各半分に文脈を足して推論,
            3: 文脈分を捨てて連結,
            4: それ以外は一括推論
          ]
          引数: [samples: float32 frames x stereo channels]
          戻り値: [入力と同じ長さのボーカル配列]
        }
        """
        import numpy as np

        length = len(samples)
        if self.split and length >= 2 * (SPLIT_CONTEXT + SPLIT_MIN_HALF):
            mid = length // 2
            left = self._infer(samples[: mid + SPLIT_CONTEXT])[:mid]
            right = self._infer(samples[mid - SPLIT_CONTEXT :])[SPLIT_CONTEXT:]
            return np.concatenate([left, right], axis=0)
        return self._infer(samples)

    def _infer(self, samples):
        """
        {
          責務: [_infer: 1ブロックをモデルへ通す。fp16はautocastで、推論はinference_mode]
          処理: [
            1: 短い入力を4096までpad,
            2: 対象デバイスへ転送,
            3: autocast+inference_modeで推論,
            4: CPUのnumpyへ戻して元の長さに切る
          ]
          引数: [samples: float32 frames x stereo channels]
          戻り値: [入力と同じ長さのボーカル配列(float32)]
        }
        """
        import numpy as np
        import torch

        length = len(samples)
        # Centered STFT uses reflect padding; very short tails still require 2049+ samples.
        if length < 4096:
            samples = np.pad(samples, ((0, 4096 - length), (0, 0)))
        tensor = torch.from_numpy(np.ascontiguousarray(samples.T)).unsqueeze(0).to(self.device)
        with torch.inference_mode(), autocast_context(self.device, self.half):
            result = self.model(tensor)[0, 0, :, :length]
        return result.float().cpu().numpy().T.copy()

    def close(self) -> None:
        if hasattr(self, "model"):
            del self.model
