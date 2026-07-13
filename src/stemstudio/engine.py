from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .core import SeparationRequest, normalize_engine_outputs


class AudioSeparatorEngine:
    def __init__(
        self,
        model_dir: str | Path,
        separator_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._separator_factory = separator_factory

    def _factory(self) -> Callable[..., Any]:
        if self._separator_factory is None:
            from audio_separator.separator import Separator

            return Separator
        return self._separator_factory

    def separate(self, request: SeparationRequest) -> list[Path]:
        separator = self._factory()(
            model_file_dir=str(self.model_dir),
            output_dir=str(request.output_dir),
            output_format=request.output_format,
            use_autocast=True,
            mdxc_params={
                "segment_size": 256,
                "override_model_segment_size": False,
                "batch_size": 1,
                "overlap": 8,
                "pitch_shift": 0,
            },
            demucs_params={
                "segment_size": "Default",
                "shifts": 2,
                "overlap": 0.25,
                "segments_enabled": True,
            },
        )
        separator.load_model(model_filename=request.model_filename)
        outputs = separator.separate(str(request.source))
        return normalize_engine_outputs(outputs, request.output_dir)


def gpu_diagnostics() -> dict[str, str | bool]:
    try:
        import torch

        available = torch.cuda.is_available()
        result: dict[str, str | bool] = {
            "available": available,
            "torch": torch.__version__,
            "cuda": str(torch.version.cuda or "不可用"),
        }
        if available:
            result.update(
                {
                    "device": torch.cuda.get_device_name(0),
                    "capability": ".".join(map(str, torch.cuda.get_device_capability(0))),
                    "vram": f"{torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB",
                }
            )
        return result
    except Exception as exc:  # diagnostics must not prevent the UI from starting
        return {"available": False, "error": str(exc)}
