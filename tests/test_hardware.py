from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stemstudio.hardware import (
    apply_hardware_config,
    detect_hardware_config,
    write_hardware_profile,
)


class FakeCuda:
    def __init__(self, devices: list[tuple[str, float, tuple[int, int]]]) -> None:
        self.devices = devices
        self.selected: int | None = None

    def is_available(self) -> bool:
        return bool(self.devices)

    def device_count(self) -> int:
        return len(self.devices)

    def get_device_name(self, index: int) -> str:
        return self.devices[index][0]

    def get_device_capability(self, index: int) -> tuple[int, int]:
        return self.devices[index][2]

    def get_device_properties(self, index: int):
        return SimpleNamespace(total_memory=self.devices[index][1] * 1024**3)

    def set_device(self, index: int) -> None:
        self.selected = index


def _torch(*devices: tuple[str, float, tuple[int, int]]):
    precision: list[str] = []
    cuda = FakeCuda(list(devices))
    return SimpleNamespace(
        __version__="2.12.0",
        version=SimpleNamespace(cuda="13.0"),
        cuda=cuda,
        set_float32_matmul_precision=precision.append,
        precision=precision,
    )


def test_detects_selected_gpu_and_balanced_defaults() -> None:
    torch = _torch(
        ("Integrated test GPU", 4.0, (8, 6)),
        ("RTX 5060 Ti", 8.0, (12, 0)),
    )

    config = detect_hardware_config(
        torch_module=torch,
        environ={"STEM_STUDIO_GPU_INDEX": "1"},
    )

    assert config.available is True
    assert config.device_index == 1
    assert config.device_name == "RTX 5060 Ti"
    assert config.vram_gb == 8.0
    assert config.tier == "balanced"
    assert config.max_live_tracks == 6
    assert config.mdxc_segment_size == 256
    assert config.gpu_concurrency == 1
    assert config.live_hop_seconds == 3
    assert config.demucs_shifts == 1
    assert config.inference_timeout_seconds == 5.5


def test_5070_ti_uses_performance_profile_and_validated_overrides() -> None:
    torch = _torch(("NVIDIA GeForce RTX 5070 Ti", 16.0, (12, 0)))

    config = detect_hardware_config(
        torch_module=torch,
        environ={
            "STEM_STUDIO_INFERENCE_TIMEOUT_SECONDS": "2.7",
        },
    )

    assert config.tier == "performance"
    assert config.max_live_tracks == 6
    assert config.mdxc_segment_size == 384
    assert config.gpu_concurrency == 2
    assert config.live_hop_seconds == 3
    assert config.demucs_shifts == 2
    assert config.shifts_benchmark_limit_seconds == 2.0
    assert config.inference_timeout_seconds == 2.7


def test_low_vram_profile_reduces_memory_pressure() -> None:
    config = detect_hardware_config(
        torch_module=_torch(("Laptop GPU", 6.0, (8, 9))),
        environ={},
    )

    assert config.tier == "compatibility"
    assert config.max_live_tracks == 4
    assert config.mdxc_segment_size == 192
    assert config.warnings


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"STEM_STUDIO_GPU_INDEX": "3"}, "GPU 索引"),
        ({"STEM_STUDIO_GPU_CONCURRENCY": "2"}, "15 GB"),
        ({"STEM_STUDIO_INFERENCE_TIMEOUT_SECONDS": "11"}, "1.0 到 10.0 秒"),
    ],
)
def test_rejects_unsafe_or_invalid_overrides(environ: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        detect_hardware_config(
            torch_module=_torch(("8GB GPU", 8.0, (9, 0))),
            environ=environ,
        )


def test_apply_and_persist_hardware_profile_atomically(tmp_path) -> None:
    torch = _torch(("GPU 0", 8.0, (9, 0)), ("GPU 1", 16.0, (12, 0)))
    config = detect_hardware_config(
        torch_module=torch,
        environ={"STEM_STUDIO_GPU_INDEX": "1"},
    )

    apply_hardware_config(config, torch_module=torch)
    path = write_hardware_profile(tmp_path, config)

    assert torch.cuda.selected == 1
    assert torch.precision == ["high"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["device_name"] == "GPU 1"
    assert payload["tier"] == "performance"
    assert not list(tmp_path.glob("*.part"))


def test_cpu_only_environment_is_reported_without_crashing() -> None:
    torch = SimpleNamespace(
        __version__="2.12.0",
        version=SimpleNamespace(cuda=None),
        cuda=FakeCuda([]),
    )

    config = detect_hardware_config(torch_module=torch, environ={})

    assert config.available is False
    assert config.tier == "unavailable"
    assert config.max_live_tracks == 0
