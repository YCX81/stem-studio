from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class HardwareConfig:
    available: bool
    device_index: int
    device_count: int
    device_name: str
    vram_gb: float
    compute_capability: str
    torch_version: str
    cuda_version: str
    tier: str
    max_live_tracks: int
    mdxc_segment_size: int
    gpu_concurrency: int
    live_hop_seconds: int
    demucs_shifts: int
    shifts_benchmark_limit_seconds: float
    inference_timeout_seconds: float
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["warnings"] = list(self.warnings)
        return payload


def _environment_value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "") or "").strip()


def _device_index(environ: Mapping[str, str], device_count: int) -> int:
    raw_value = _environment_value(environ, "STEM_STUDIO_GPU_INDEX") or "0"
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("STEM_STUDIO_GPU_INDEX 必须是有效的 GPU 索引。") from exc
    if not 0 <= value < device_count:
        raise ValueError(f"GPU 索引 {value} 超出容器内 {device_count} 张可见显卡的范围。")
    return value


def _tier_for_vram(vram_gb: float) -> tuple[str, int, int, tuple[str, ...]]:
    if vram_gb < 5.5:
        return (
            "limited",
            2,
            128,
            ("显存低于 5.5 GB，仅建议使用二轨模式；首次推理可能回退原声。",),
        )
    if vram_gb < 7.5:
        return (
            "compatibility",
            4,
            192,
            ("显存低于 7.5 GB，已缩小文件分离片段并隐藏六轨实时模式。",),
        )
    if vram_gb < 12.0:
        return "balanced", 6, 256, ()
    return "performance", 6, 384, ()


def _gpu_concurrency(environ: Mapping[str, str], vram_gb: float) -> int:
    raw_value = _environment_value(environ, "STEM_STUDIO_GPU_CONCURRENCY")
    if not raw_value or raw_value.casefold() == "auto":
        return 2 if vram_gb >= 15.0 else 1
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("STEM_STUDIO_GPU_CONCURRENCY 必须是 1、2 或 auto。") from exc
    if value not in {1, 2}:
        raise ValueError("STEM_STUDIO_GPU_CONCURRENCY 必须是 1、2 或 auto。")
    if value == 2 and vram_gb < 15.0:
        raise ValueError("并发 2 个 GPU 文件任务至少需要 15 GB 可见显存。")
    return value


def _inference_timeout(environ: Mapping[str, str]) -> float:
    raw_value = _environment_value(
        environ, "STEM_STUDIO_INFERENCE_TIMEOUT_SECONDS"
    )
    if not raw_value or raw_value.casefold() == "auto":
        return 2.8
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError("实时推理截止时间必须是 1.0 到 2.9 秒。") from exc
    if not math.isfinite(value) or not 1.0 <= value <= 2.9:
        raise ValueError("3 秒步进的实时推理截止时间必须小于 3 秒。")
    return round(value, 3)


def detect_hardware_config(
    *,
    torch_module: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> HardwareConfig:
    if torch_module is None:
        import torch as torch_module

    resolved_environment = os.environ if environ is None else environ
    torch_version = str(getattr(torch_module, "__version__", "未知"))
    cuda_version = str(getattr(getattr(torch_module, "version", None), "cuda", None) or "不可用")
    cuda = torch_module.cuda
    if not cuda.is_available():
        return HardwareConfig(
            available=False,
            device_index=0,
            device_count=0,
            device_name="",
            vram_gb=0.0,
            compute_capability="",
            torch_version=torch_version,
            cuda_version=cuda_version,
            tier="unavailable",
            max_live_tracks=0,
            mdxc_segment_size=128,
            gpu_concurrency=1,
            live_hop_seconds=3,
            demucs_shifts=1,
            shifts_benchmark_limit_seconds=2.4,
            inference_timeout_seconds=2.8,
            warnings=("容器未检测到可用 CUDA GPU。",),
        )

    device_count = int(cuda.device_count())
    index = _device_index(resolved_environment, device_count)
    properties = cuda.get_device_properties(index)
    vram_gb = round(float(properties.total_memory) / 1024**3, 2)
    tier, max_live_tracks, segment_size, warnings = _tier_for_vram(vram_gb)
    capability = ".".join(map(str, cuda.get_device_capability(index)))
    return HardwareConfig(
        available=True,
        device_index=index,
        device_count=device_count,
        device_name=str(cuda.get_device_name(index)),
        vram_gb=vram_gb,
        compute_capability=capability,
        torch_version=torch_version,
        cuda_version=cuda_version,
        tier=tier,
        max_live_tracks=max_live_tracks,
        mdxc_segment_size=segment_size,
        gpu_concurrency=_gpu_concurrency(resolved_environment, vram_gb),
        live_hop_seconds=3,
        demucs_shifts=2 if vram_gb >= 15.0 else 1,
        shifts_benchmark_limit_seconds=2.4,
        inference_timeout_seconds=_inference_timeout(resolved_environment),
        warnings=warnings,
    )


def apply_hardware_config(
    config: HardwareConfig, *, torch_module: Any | None = None
) -> None:
    if not config.available:
        return
    if torch_module is None:
        import torch as torch_module

    torch_module.cuda.set_device(config.device_index)
    set_precision = getattr(torch_module, "set_float32_matmul_precision", None)
    if callable(set_precision):
        set_precision("high")


def write_hardware_profile(
    data_root: str | Path, config: HardwareConfig
) -> Path:
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "hardware-profile.json"
    partial = destination.with_suffix(".json.part")
    payload = {
        "version": 1,
        **config.to_dict(),
        "updated_at_ns": time.time_ns(),
    }
    partial.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(partial, destination)
    return destination
