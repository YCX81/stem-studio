from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


MODEL_PROFILES = {
    "人声 / 伴奏 · 高质量": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
    "四轨 · 人声/鼓/贝斯/其他": "htdemucs_ft.yaml",
    "六轨 · 加吉他/钢琴": "htdemucs_6s.yaml",
}


@dataclass(frozen=True)
class LiveProfile:
    name: str
    model_filename: str
    stems: tuple[str, ...]


LIVE_PROFILES = {
    "人声 / 伴奏 · 高质量": LiveProfile(
        name="人声 / 伴奏 · 高质量",
        model_filename=MODEL_PROFILES["人声 / 伴奏 · 高质量"],
        stems=("vocals", "instrumental"),
    ),
    "四轨 · 人声/鼓/贝斯/其他": LiveProfile(
        name="四轨 · 人声/鼓/贝斯/其他",
        model_filename=MODEL_PROFILES["四轨 · 人声/鼓/贝斯/其他"],
        stems=("vocals", "drums", "bass", "other"),
    ),
    "六轨 · 加吉他/钢琴": LiveProfile(
        name="六轨 · 加吉他/钢琴",
        model_filename=MODEL_PROFILES["六轨 · 加吉他/钢琴"],
        stems=("vocals", "drums", "bass", "guitar", "piano", "other"),
    ),
}

SUPPORTED_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".wma",
    ".aiff",
}
SUPPORTED_OUTPUT_FORMATS = {"FLAC", "WAV", "MP3"}


@dataclass(frozen=True)
class SeparationRequest:
    source: Path
    profile_name: str
    model_filename: str
    output_format: str
    output_dir: Path

    @classmethod
    def create(
        cls,
        source: str | Path,
        profile_name: str,
        output_format: str,
        output_root: str | Path,
    ) -> "SeparationRequest":
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError("音频文件不存在，请重新选择。")
        if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的音频格式：{source_path.suffix or '无扩展名'}")
        if profile_name not in MODEL_PROFILES:
            raise ValueError("未知分离模式，请重新选择。")

        normalized_format = output_format.upper()
        if normalized_format not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError("输出格式仅支持 FLAC、WAV 或 MP3。")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output_dir = Path(output_root).expanduser().resolve() / f"{source_path.stem}-{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=False)
        return cls(
            source=source_path,
            profile_name=profile_name,
            model_filename=MODEL_PROFILES[profile_name],
            output_format=normalized_format,
            output_dir=output_dir,
        )


def normalize_engine_outputs(outputs: Iterable[str | Path], output_dir: Path) -> list[Path]:
    normalized: list[Path] = []
    for raw_path in outputs:
        path = Path(raw_path)
        if not path.is_absolute():
            path = output_dir / path
        path = path.resolve()
        if path.is_file():
            normalized.append(path)
    if not normalized:
        raise RuntimeError("分离引擎没有生成可用的音轨文件。")
    return normalized
