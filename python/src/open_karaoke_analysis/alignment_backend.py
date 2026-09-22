"""Lazy Japanese Wav2Vec2 acoustic backend, isolated from the alignment algorithm."""

import hashlib
import importlib.metadata
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import atomic_json, resolve_artifact
from .contracts import ServiceError

MODEL_ID = "jonatasgrosman/wav2vec2-large-xlsr-53-japanese"
REVISION = "cf031e020336460d15a417eba710bbc5bb43be9a"
WEIGHT_SHA256 = "4f6821dcd79770fcd4c11487c4ee5c040b3a7e1863425224014ba5ea8fbc1b67"
TRANSFORMERS_VERSION = "4.57.1"
FILES = {"config.json": 131_072, "preprocessor_config.json": 16_384,
         "vocab.json": 131_072, "pytorch_model.bin": 1_500_000_000}


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ServiceError("model_download", "Insecure alignment-model redirect", 502)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def digest_file(path: Path, context, limit: int) -> str:
    if not path.is_file() or path.stat().st_size > limit:
        raise ServiceError("model_integrity", "Invalid cached alignment-model file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            context.checkpoint()
            digest.update(block)
    return digest.hexdigest()


def download_file(context, directory: Path, name: str, limit: int) -> str:
    """Only fixed upstream names/revision; downloads publish by atomic replacement."""
    url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}"
    opener = urllib.request.build_opener(HttpsRedirect())
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=directory)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as output:
            request = urllib.request.Request(url, headers={"User-Agent": "OpenKaraoke/0.1"})
            try:
                with opener.open(request, timeout=60) as response:
                    if urlsplit(response.url).scheme != "https":
                        raise ServiceError("model_download", "Insecure model response", 502)
                    while block := response.read(1024 * 1024):
                        context.checkpoint()
                        size += len(block)
                        if size > limit:
                            raise ServiceError("model_integrity", "Alignment-model file too large")
                        digest.update(block)
                        output.write(block)
            except (urllib.error.URLError, TimeoutError) as error:
                raise ServiceError("model_download", "Alignment model download failed", 503, True) from error
            output.flush()
            os.fsync(output.fileno())
        if name == "pytorch_model.bin" and digest.hexdigest() != WEIGHT_SHA256:
            raise ServiceError("model_integrity", "Alignment checkpoint SHA-256 mismatch")
        context.checkpoint()
        os.replace(temporary, directory / name)
        return digest.hexdigest()
    finally:
        Path(temporary).unlink(missing_ok=True)


def model_snapshot(context, allow_download: bool) -> tuple[Path, dict]:
    relative = f"models/alignment-ja/{REVISION}"
    directory = resolve_artifact(context.root, relative)
    manifest_path = resolve_artifact(context.root, f"{relative}/snapshot.json")
    paths = {name: resolve_artifact(context.root, f"{relative}/{name}") for name in FILES}
    context.checkpoint()
    if manifest_path.is_file():
        try:
            if manifest_path.stat().st_size > 8192:
                raise ValueError("oversized manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["revision"] != REVISION or set(manifest["sha256"]) != set(FILES):
                raise ValueError("wrong snapshot")
            for name, limit in FILES.items():
                expected = WEIGHT_SHA256 if name == "pytorch_model.bin" else manifest["sha256"][name]
                if digest_file(paths[name], context, limit) != expected:
                    raise ValueError("invalid hash")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ServiceError("model_integrity", "Alignment-model snapshot is incomplete or corrupt") from error
        return directory, manifest
    if not allow_download:
        raise ServiceError("model_not_installed", "A verified alignment model is required; downloads disabled", 503)
    directory.mkdir(parents=True, exist_ok=True)
    context.report(0.03, "downloading_alignment_model")
    hashes = {name: download_file(context, directory, name, limit) for name, limit in FILES.items()}
    manifest = {"model": MODEL_ID, "revision": REVISION, "sha256": hashes}
    atomic_json(manifest_path, manifest)
    return directory, manifest


class JapaneseCtcBackend:
    """Protocol: prepare(context, options), emissions(mono16k), identity(), close()."""

    blank_id = 0

    def __init__(self):
        self.vocabulary: dict[str, int] = {}
        self.snapshot = None

    def identity(self) -> dict:
        packages = {}
        for name in ("transformers", "torch", "numpy", "scipy", "soundfile"):
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = None
        return {"id": MODEL_ID, "revision": REVISION, "weights_sha256": WEIGHT_SHA256,
                "weights_license": "Apache-2.0", "code_license": "Apache-2.0",
                "model_sample_rate": 16_000, "runtime_versions": packages,
                "snapshot": self.snapshot, "device": getattr(self, "device", None)}

    def prepare(self, context, options: dict) -> None:
        try:
            if importlib.metadata.version("transformers") != TRANSFORMERS_VERSION:
                raise ServiceError("incompatible_runtime", "Requires transformers 4.57.1", 503)
            import torch
            from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2ForCTC
        except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as error:
            raise ServiceError("dependency_missing", "Install the analysis alignment extra", 503) from error
        if options["device"] == "cuda" and not torch.cuda.is_available():
            raise ServiceError("device_unavailable", "CUDA is unavailable", 503)
        directory, self.snapshot = model_snapshot(context, options["allow_model_download"])
        context.checkpoint()
        context.report(0.08, "loading_alignment_model")
        try:
            # Explicit classes + local-only loading: no remote Python or unsafe pickle fallback.
            model, details = Wav2Vec2ForCTC.from_pretrained(
                directory, local_files_only=True, use_safetensors=False,
                weights_only=True, output_loading_info=True,
            )
            if any(details.get(key) for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
                raise ValueError("Checkpoint does not exactly match the model")
            self.processor = Wav2Vec2FeatureExtractor.from_pretrained(directory, local_files_only=True)
            vocab = json.loads((directory / "vocab.json").read_text(encoding="utf-8"))
            if (self.processor.sampling_rate != 16_000 or model.config.pad_token_id != 0
                    or len(vocab) != model.config.vocab_size
                    or set(vocab.values()) != set(range(model.config.vocab_size))):
                raise ValueError("Unsupported acoustic model layout")
            self.vocabulary = {char.casefold(): index for char, index in vocab.items() if len(char) == 1}
            self.model = model.eval().to(options["device"])
            self.device = options["device"]
        except (ValueError, RuntimeError, OSError, KeyError) as error:
            raise ServiceError("model_load_failed", "Could not load the verified alignment model", 503) from error
        context.checkpoint()

    def emissions(self, samples):
        import torch

        encoded = self.processor(samples, sampling_rate=16_000, return_tensors="pt")
        with torch.inference_mode():
            logits = self.model(**{key: value.to(self.device) for key, value in encoded.items()}).logits
        return logits[0].float().log_softmax(dim=-1).cpu().numpy()

    def close(self) -> None:
        if hasattr(self, "model"):
            del self.model
