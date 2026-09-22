import pytest

from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.stem_backend import spectral_state_dict


def test_legacy_transform_buffers_are_removed_without_mutation():
    weight = object()
    state = {
        "sample_rate": 44100,
        "stft.window": object(),
        "transform.0.window": object(),
        "fc1.weight": weight,
    }
    migrated = spectral_state_dict(state)
    assert migrated == {"fc1.weight": weight}
    assert len(state) == 4


def test_unknown_keys_remain_for_strict_loader_to_reject():
    state = {"unexpected.weight": object(), "fc1.weight": object()}
    assert spectral_state_dict(state) == state


@pytest.mark.parametrize("key", ["sample_rate", "stft.window", "transform.0.window"])
def test_partial_legacy_checkpoint_is_rejected(key):
    with pytest.raises(ServiceError) as caught:
        spectral_state_dict({key: object(), "fc1.weight": object()})
    assert caught.value.detail.code == "model_incompatible"
