"""device=auto/GPU優先とOOMフォールバックの単体テスト。torchはモックで、実モデル推論はしない。"""

import contextlib
import sys
import types

import numpy as np
import pytest

from open_karaoke_analysis.alignment import AlignmentOptions
from open_karaoke_analysis.alignment_backend import JapaneseCtcBackend
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.device import (
    autocast_context,
    is_oom,
    resolve_device,
    run_with_oom_fallback,
)
from open_karaoke_analysis.lyrics import LyricOptions
from open_karaoke_analysis.stem_backend import UmxHqBackend
from open_karaoke_analysis.stems import StemOptions
from open_karaoke_analysis.whisper_backend import WhisperBackend


class FakeOom(RuntimeError):
    """torch.cuda.OutOfMemoryErrorの代役(型名で判定される)。"""


FakeOom.__name__ = "OutOfMemoryError"


class FakeTensor:
    """numpy配列を包む最小のテンソル代役。"""

    def __init__(self, array):
        self.array = np.asarray(array)

    def to(self, device):
        return self

    def float(self):
        return FakeTensor(self.array.astype("float32"))

    def log_softmax(self, dim):
        shifted = self.array - self.array.max(axis=dim, keepdims=True)
        return FakeTensor(shifted - np.log(np.exp(shifted).sum(axis=dim, keepdims=True)))

    def cpu(self):
        return self

    def numpy(self):
        return self.array

    def __getitem__(self, key):
        return FakeTensor(self.array[key])


def make_torch(cuda: bool):
    """cuda可否を指定できるtorch代役。autocast呼出しを記録する。"""
    module = types.ModuleType("torch")
    module.calls = []
    module.cuda = types.SimpleNamespace(
        is_available=lambda: cuda, empty_cache=lambda: module.calls.append("empty_cache")
    )
    module.inference_mode = contextlib.nullcontext

    @contextlib.contextmanager
    def autocast(device, dtype=None):
        module.calls.append(("autocast", device, dtype))
        try:
            yield
        finally:
            module.calls.append(("autocast_exit",))

    module.autocast = autocast
    module.float16 = "float16"
    return module


@pytest.fixture
def cuda_torch(monkeypatch):
    module = make_torch(True)
    monkeypatch.setitem(sys.modules, "torch", module)
    return module


@pytest.fixture
def cpu_torch(monkeypatch):
    module = make_torch(False)
    monkeypatch.setitem(sys.modules, "torch", module)
    return module


def test_resolve_device_auto_follows_cuda(cuda_torch, monkeypatch):
    assert resolve_device("auto") == "cuda"
    monkeypatch.setitem(sys.modules, "torch", make_torch(False))
    assert resolve_device("auto") == "cpu"


def test_resolve_device_auto_without_torch_is_cpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # import失敗を再現
    assert resolve_device("auto") == "cpu"


@pytest.mark.parametrize("explicit", ["cpu", "cuda"])
def test_explicit_device_is_respected(cuda_torch, explicit):
    assert resolve_device(explicit) == explicit


@pytest.mark.parametrize(
    "schema,extra",
    [
        (StemOptions, {}),
        (LyricOptions, {}),
        (AlignmentOptions, {"lyrics_artifact": "x.json"}),
    ],
)
def test_options_default_auto_resolves(schema, extra, cuda_torch, monkeypatch):
    assert schema(**extra).device == "cuda"
    assert schema(device="cpu", **extra).device == "cpu"
    monkeypatch.setitem(sys.modules, "torch", make_torch(False))
    assert schema(**extra).device == "cpu"
    assert schema(device="cuda", **extra).device == "cuda"


def test_is_oom_detects_type_and_message():
    assert is_oom(FakeOom("x"))
    assert is_oom(RuntimeError("CUDA out of memory. Tried to allocate"))
    assert not is_oom(RuntimeError("shape mismatch"))
    assert not is_oom(ValueError("out of memory"))


def raiser(error):
    def raise_it(_stage):
        raise error

    return raise_it


def test_ladder_advances_only_on_oom(cuda_torch):
    tried = []

    def run(stage):
        tried.append(stage)
        if stage != "cpu":
            raise FakeOom("oom")
        return "ok"

    stages = [("a", None), ("b", lambda: tried.append("switch-b")), ("cpu", None)]
    assert run_with_oom_fallback(stages, run) == "ok"
    assert tried == ["a", "switch-b", "b", "cpu"]
    assert cuda_torch.calls.count("empty_cache") == 2


def test_ladder_reraises_non_oom_and_last_oom(cuda_torch):
    stages = [("a", None), ("b", None)]
    with pytest.raises(ValueError):
        run_with_oom_fallback(stages, raiser(ValueError()))
    with pytest.raises(FakeOom):
        run_with_oom_fallback(stages, raiser(FakeOom()))


def test_autocast_only_on_cuda_half(cuda_torch):
    with autocast_context("cuda", True):
        pass
    with autocast_context("cuda", False), autocast_context("cpu", True):
        pass
    assert cuda_torch.calls == [("autocast", "cuda", "float16"), ("autocast_exit",)]


class FakeUmxModel:
    """デバイス移動だけを記録するモデル代役。"""

    def __init__(self):
        self.device, self.log = "cuda", []

    def to(self, device):
        self.device = device
        return self


def umx_backend(limit):
    """cudaでは長さlimit超かfp16のとき、CPUでは常に成功する代役バックエンド。"""
    backend = UmxHqBackend()
    backend.model = FakeUmxModel()
    backend.device, backend.half, backend.split = "cuda", True, False

    def infer(samples):
        backend.model.log.append((backend.device, backend.half, backend.split, len(samples)))
        if backend.device == "cuda" and (backend.half or len(samples) > limit):
            raise FakeOom("oom")
        return (samples * 0.5).astype("float32")

    backend._infer = infer
    return backend


SAMPLES = np.random.default_rng(0).standard_normal((44_100 * 12, 2)).astype("float32")


def test_umx_split_equals_whole(cuda_torch):
    # fp16段階はOOM、分割(split)段階で半分の長さなら成功し、結果は一括推論と同じ
    backend = umx_backend(limit=len(SAMPLES) // 2 + 44_100)
    backend.vocals_result = backend.vocals(SAMPLES)
    np.testing.assert_allclose(backend.vocals_result, SAMPLES * 0.5)
    assert backend.split and backend.device == "cuda"


def test_umx_falls_back_to_cpu(cuda_torch):
    # 分割してもfp32でもOOMなら最後にCPUで成功する
    backend = umx_backend(limit=10)
    out = backend.vocals(SAMPLES)
    np.testing.assert_allclose(out, SAMPLES * 0.5)
    assert (backend.device, backend.half, backend.split) == ("cpu", False, False)
    assert backend.model.device == "cpu"


def test_umx_cpu_has_no_fallback_stage(cpu_torch):
    backend = umx_backend(limit=10)
    backend.device = "cpu"
    out = backend.vocals(SAMPLES[:1000])
    assert len(out) == 1000
    assert [entry[0] for entry in backend.model.log] == ["cpu"]


class FakeWhisper:
    """fp16かbeam>1のときcudaでOOMになるWhisper代役。"""

    def __init__(self):
        self.device, self.calls = "cuda", []

    def float(self):
        return self

    def to(self, device):
        self.device = device
        return self

    def transcribe(self, audio, **kwargs):
        self.calls.append((self.device, kwargs["fp16"], kwargs["beam_size"]))
        if self.device == "cuda" and (kwargs["fp16"] or kwargs["beam_size"] > 1):
            raise FakeOom("oom")
        return {"text": "ok", "segments": []}


def whisper_backend(device):
    backend = WhisperBackend()
    backend.model = FakeWhisper()
    backend.model.device = device
    backend.device, backend.half = device, device == "cuda"
    return backend


OPTIONS = {"language": "ja", "beam_size": 5, "device": "cuda"}
AUDIO = np.zeros(16_000, dtype="float32")


def test_whisper_falls_back_beam_then_fp32(cuda_torch):
    backend = whisper_backend("cuda")
    assert backend.transcribe(AUDIO, OPTIONS)["text"] == "ok"
    # fp16 beam5 -> fp16 beam1 -> fp32 beam1 で成功し、CPUには落ちない
    assert backend.model.calls == [("cuda", True, 5), ("cuda", True, 1), ("cuda", False, 1)]
    assert backend.device == "cuda" and backend.half is False


def test_whisper_cpu_is_single_stage(cpu_torch):
    backend = whisper_backend("cpu")
    backend.transcribe(AUDIO, {**OPTIONS, "device": "cpu"})
    assert backend.model.calls == [("cpu", False, 5)]


def test_whisper_all_oom_becomes_inference_failed(cuda_torch):
    backend = whisper_backend("cuda")

    def always_oom(audio, **kwargs):
        raise FakeOom("oom")

    backend.model.transcribe = always_oom
    with pytest.raises(ServiceError) as error:
        backend.transcribe(AUDIO, OPTIONS)
    assert error.value.detail.code == "inference_failed"


class FakeWav2Vec:
    """cudaでは常にOOMになる整列モデル代役。呼出し時のautocast状態を記録する。"""

    def __init__(self, torch_module):
        self.torch, self.device = torch_module, "cuda"

    def to(self, device):
        self.device = device
        return self

    def __call__(self, **inputs):
        entered = [c[0] for c in self.torch.calls if c[0] in ("autocast", "autocast_exit")]
        in_autocast = entered.count("autocast") > entered.count("autocast_exit")
        self.torch.calls.append(("forward", self.device, in_autocast))
        if self.device == "cuda":
            raise FakeOom("oom")
        return types.SimpleNamespace(logits=FakeTensor(np.ones((1, 4, 3))))


def test_alignment_emissions_fall_back_to_cpu(cuda_torch):
    backend = JapaneseCtcBackend()
    backend.model, backend.device, backend.half = FakeWav2Vec(cuda_torch), "cuda", True

    def processor(samples, sampling_rate, return_tensors):
        return {"input_values": FakeTensor(samples)}

    backend.processor = processor
    out = backend.emissions(AUDIO)
    assert out.shape == (4, 3)
    np.testing.assert_allclose(np.exp(out).sum(axis=-1), 1.0, rtol=1e-5)
    assert backend.device == "cpu" and backend.half is False
    forwards = [call for call in cuda_torch.calls if call[0] == "forward"]
    # fp16(autocast)中のcuda -> fp32のcuda -> cpu の順
    assert [(f[1], f[2]) for f in forwards] == [("cuda", True), ("cuda", False), ("cpu", False)]
