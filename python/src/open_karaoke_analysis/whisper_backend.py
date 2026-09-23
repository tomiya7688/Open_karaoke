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
from .contracts import ServiceError

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
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            context.checkpoint()
            digest.update(block)
    context.checkpoint()
    return digest.hexdigest()


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
            self.model = model.to(options["device"]).eval()
        except (RuntimeError, KeyError, TypeError, ValueError) as error:
            raise ServiceError(
                "model_load", "Verified Whisper checkpoint could not be loaded", 503
            ) from error
        context.checkpoint()

    def transcribe(self, audio, options: dict) -> dict:
        if self.model is None:
            raise ServiceError("model_not_ready", "Whisper is not ready", 503)
        try:
            # Array input is already mono/16 kHz: no FFmpeg subprocess or network audio I/O.
            # Keep low-confidence raw hypotheses; the adapter flags them separately.
            return self.model.transcribe(
                audio,
                language=options["language"],
                task="transcribe",
                temperature=0.0,
                beam_size=options["beam_size"],
                condition_on_previous_text=False,
                word_timestamps=False,
                fp16=options["device"] == "cuda",
                verbose=None,
                no_speech_threshold=None,
                logprob_threshold=None,
                compression_ratio_threshold=None,
            )
        except (RuntimeError, ValueError) as error:
            raise ServiceError("inference_failed", "Whisper transcription failed", 500) from error

    def close(self) -> None:
        self.model = None
