"""Pinned, lazily loaded UMX-HQ vocals model. No torch.hub code execution."""

import hashlib
import importlib.metadata
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ServiceError

MODEL_ID = "umxhq-vocals"
WEIGHT_VERSION = "1.0.1"
WEIGHT_FILE = "vocals-b62c91ce.pth"
WEIGHT_SHA256 = "b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083"
WEIGHT_BYTES = 35_637_796
WEIGHT_URL = f"https://zenodo.org/api/records/3370489/files/{WEIGHT_FILE}/content"
MODEL_RATE = 44_100


def file_sha256(path: Path, context: AnalysisContext) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            context.checkpoint()
            digest.update(block)
    context.checkpoint()
    return digest.hexdigest()


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
        targets["vocals"].load_state_dict(state, strict=True)
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
        context.checkpoint()

    def vocals(self, samples):
        """Input/output: float32 frames x stereo channels at 44.1 kHz."""
        import numpy as np
        import torch

        length = len(samples)
        # Centered STFT uses reflect padding; very short tails still require 2049+ samples.
        if length < 4096:
            samples = np.pad(samples, ((0, 4096 - length), (0, 0)))
        tensor = torch.from_numpy(np.ascontiguousarray(samples.T)).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            result = self.model(tensor)[0, 0, :, :length].cpu().numpy().T.copy()
        return result

    def close(self) -> None:
        if hasattr(self, "model"):
            del self.model
