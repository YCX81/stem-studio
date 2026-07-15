from __future__ import annotations

import json
import os
import re
import sys
import time
import wave
from array import array
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Protocol


_chunk_pattern = re.compile(r"^capture-(\d{8})\.wav$")


@dataclass(frozen=True)
class LiveConfig:
    sample_rate: int = 44_100
    channels: int = 2
    window_seconds: int = 12
    hop_seconds: int = 6
    stable_offset_seconds: int = 0
    crossfade_milliseconds: int = 100

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("采样率必须为正数。")
        if self.channels != 2:
            raise ValueError("高质量流式模式当前仅支持双声道。")
        if self.window_seconds < 8:
            raise ValueError("窗口不得短于 8 秒。")
        if self.hop_seconds <= 0:
            raise ValueError("步长必须为正数。")
        if self.window_seconds % self.hop_seconds:
            raise ValueError("窗口长度必须能被步长整除。")
        if self.crossfade_milliseconds <= 0 or self.overlap_frames > self.hop_frames:
            raise ValueError("交叉淡化长度必须大于零且不超过一个步长。")
        stable_end_frame = (
            self.stable_offset_seconds * self.sample_rate + self.output_frames
        )
        if self.stable_offset_seconds < 0 or stable_end_frame > self.window_frames:
            raise ValueError("稳定区间必须完整位于窗口内。")

    @property
    def window_frames(self) -> int:
        return self.sample_rate * self.window_seconds

    @property
    def hop_frames(self) -> int:
        return self.sample_rate * self.hop_seconds

    @property
    def overlap_frames(self) -> int:
        return max(1, self.sample_rate * self.crossfade_milliseconds // 1_000)

    @property
    def output_frames(self) -> int:
        return self.hop_frames + self.overlap_frames


@dataclass(frozen=True)
class ReadyChunk:
    sequence: int
    path: Path


def discover_ready_chunks(directory: str | Path, after_sequence: int = 0) -> list[ReadyChunk]:
    root = Path(directory)
    if not root.is_dir():
        return []
    chunks: list[ReadyChunk] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        match = _chunk_pattern.fullmatch(path.name)
        if match is None:
            continue
        sequence = int(match.group(1))
        if sequence > after_sequence:
            chunks.append(ReadyChunk(sequence=sequence, path=path.resolve()))
    return sorted(chunks, key=lambda chunk: chunk.sequence)


class SeparatorLike(Protocol):
    def separate(self, source: Path) -> list[Path]: ...


class _ResidentDemucsRuntime:
    """Run Demucs repeatedly without moving weights on/off CUDA per window."""

    def __init__(self, architecture: Any) -> None:
        from audio_separator.separator.architectures.demucs_separator import (
            DEMUCS_2_SOURCE_MAPPER,
            DEMUCS_4_SOURCE_MAPPER,
            DEMUCS_6_SOURCE_MAPPER,
            demucs_segments,
            get_demucs_model,
        )

        self._architecture = architecture
        self._source_maps = {
            2: DEMUCS_2_SOURCE_MAPPER,
            4: DEMUCS_4_SOURCE_MAPPER,
            6: DEMUCS_6_SOURCE_MAPPER,
        }
        model_path = Path(architecture.model_path).resolve()
        model = get_demucs_model(name=model_path.stem, repo=model_path.parent)
        model = demucs_segments(architecture.segment_size, model)
        model.to(architecture.torch_device)
        model.eval()
        self._model = model
        architecture.demucs_model_instance = model

    def separate(self, source: Path) -> list[Path]:
        architecture = self._architecture
        architecture.audio_file_path = str(source)
        architecture.audio_file_base = source.stem
        mix = architecture.prepare_mix(str(source))
        separated = architecture.demix_demucs(mix)
        try:
            source_map = self._source_maps[len(separated)]
        except KeyError as error:
            raise RuntimeError(
                f"实时 Demucs 返回了不支持的 {len(separated)} 个源音轨。"
            ) from error
        architecture.demucs_source_map = source_map
        outputs: list[Path] = []
        for stem_name, stem_index in source_map.items():
            stem_path = Path(architecture.get_stem_output_path(stem_name, None))
            architecture.final_process(
                str(stem_path),
                separated[stem_index].T,
                stem_name,
            )
            outputs.append(stem_path)
        return outputs

    def close(self) -> None:
        architecture = self._architecture
        architecture.demucs_model_instance = None
        self._model = None
        architecture.clear_gpu_cache()


class PersistentSeparator:
    """Keep one GPU model resident while processing successive live windows."""

    def __init__(
        self,
        model_dir: str | Path,
        work_dir: str | Path,
        model_filename: str,
        separator_factory: Callable[..., Any] | None = None,
        resident_demucs_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.model_dir = Path(model_dir).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.model_filename = model_filename
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if separator_factory is None:
            from audio_separator.separator import Separator

            separator_factory = Separator
        self._separator = separator_factory(
            model_file_dir=str(self.model_dir),
            output_dir=str(self.work_dir),
            output_format="WAV",
            use_autocast=True,
            mdxc_params={
                "segment_size": 256,
                "override_model_segment_size": False,
                "batch_size": 1,
                "overlap": 4,
                "pitch_shift": 0,
            },
            demucs_params={
                "segment_size": "Default",
                "shifts": 1,
                "overlap": 0.25,
                "segments_enabled": True,
            },
        )
        self._separator.load_model(model_filename=model_filename)
        self._resident_runtime = None
        architecture = getattr(self._separator, "model_instance", None)
        if model_filename.casefold().startswith("htdemucs") and architecture is not None:
            runtime_factory = resident_demucs_factory or _ResidentDemucsRuntime
            self._resident_runtime = runtime_factory(architecture)

    def separate(self, source: Path) -> list[Path]:
        outputs = (
            self._resident_runtime.separate(source)
            if self._resident_runtime is not None
            else self._separator.separate(str(source))
        )
        return [
            path if path.is_absolute() else self.work_dir / path
            for path in (Path(output) for output in outputs)
        ]

    def close(self) -> None:
        if self._resident_runtime is not None:
            self._resident_runtime.close()
            self._resident_runtime = None


@dataclass(frozen=True)
class ProcessedChunk:
    sequence: int
    manifest: Path
    latency_seconds: float


class LiveChunkProcessor:
    """Turn one overlapping capture window into a stable, playable hop."""

    def __init__(
        self,
        config: LiveConfig,
        outbox: str | Path,
        separator: SeparatorLike,
        expected_stems: tuple[str, ...] = ("vocals", "instrumental"),
        stem_sources: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.config = config
        self.outbox = Path(outbox).resolve()
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.separator: SeparatorLike | None = separator
        self.expected_stems = expected_stems
        if not expected_stems or len(set(expected_stems)) != len(expected_stems):
            raise ValueError("实时输出音轨定义无效。")
        requested_sources = stem_sources or {stem: (stem,) for stem in expected_stems}
        if set(requested_sources) != set(expected_stems):
            raise ValueError("实时模型源分组必须覆盖全部输出音轨。")
        self.stem_sources = {
            stem: tuple(requested_sources[stem])
            for stem in expected_stems
        }
        if any(not sources for sources in self.stem_sources.values()):
            raise ValueError("实时模型源分组不能为空。")
        flattened = tuple(source for sources in self.stem_sources.values() for source in sources)
        if len(flattened) != len(set(flattened)):
            raise ValueError("同一模型源音轨不能重复汇入多个输出音轨。")

    def close(self) -> None:
        if self.separator is None:
            return
        close = getattr(self.separator, "close", None)
        if callable(close):
            close()

    def detach_separator(self) -> SeparatorLike:
        if self.separator is None:
            raise RuntimeError("实时分离器已经从窗口处理器移出。")
        separator = self.separator
        self.separator = None
        return separator

    def process(self, chunk: ReadyChunk) -> ProcessedChunk:
        started = time.time()
        if self.separator is None:
            raise RuntimeError("实时窗口处理器没有可用的分离器。")
        outputs = self.separator.separate(chunk.path)
        stems = self._identify_stems(outputs)
        published: dict[str, str] = {}
        for stem_name, sources in stems.items():
            filename = f"result-{chunk.sequence:08d}-{stem_name}.wav"
            destination = self.outbox / filename
            self._write_overlap_hop(sources, destination)
            published[stem_name] = filename

        # The capture file only becomes visible after its complete window is written.
        latency = self.config.window_seconds + max(0.0, time.time() - chunk.path.stat().st_mtime)
        manifest = self.outbox / f"result-{chunk.sequence:08d}.json"
        partial = manifest.with_suffix(".json.part")
        payload = {
            "version": 2,
            "sequence": chunk.sequence,
            "sample_rate": self.config.sample_rate,
            "channels": self.config.channels,
            "window_seconds": self.config.window_seconds,
            "hop_seconds": self.config.hop_seconds,
            "overlap_frames": self.config.overlap_frames,
            "processing_seconds": round(time.time() - started, 3),
            "latency_seconds": round(latency, 3),
            "cache_hit": False,
            "stems": published,
        }
        partial.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, manifest)
        return ProcessedChunk(chunk.sequence, manifest, latency)

    def _identify_stems(self, outputs: list[Path]) -> dict[str, tuple[Path, ...]]:
        candidates: dict[str, Path] = {}
        for path in outputs:
            name = path.stem.casefold()
            if "instrumental" in name or "no_vocals" in name:
                candidates["instrumental"] = path
                continue
            for stem in ("vocals", "drums", "bass", "guitar", "piano", "other"):
                if stem in name:
                    candidates[stem] = path
                    break
        required_sources = tuple(
            source
            for stem in self.expected_stems
            for source in self.stem_sources[stem]
        )
        missing = [stem for stem in required_sources if stem not in candidates]
        if missing:
            if self.stem_sources == {
                "vocals": ("vocals",),
                "instrumental": ("instrumental",),
            }:
                raise RuntimeError("实时分离必须同时产生人声和伴奏两个音轨。")
            raise RuntimeError(f"实时分离缺少模型源音轨：{', '.join(missing)}")
        missing_files = [stem for stem in required_sources if not candidates[stem].is_file()]
        if missing_files:
            raise RuntimeError(f"模型输出文件不存在：{', '.join(missing_files)}")
        return {
            stem: tuple(candidates[source] for source in self.stem_sources[stem])
            for stem in self.expected_stems
        }

    def _write_overlap_hop(self, sources: tuple[Path, ...], destination: Path) -> None:
        partial = destination.with_suffix(".wav.part")
        chunks: list[bytes] = []
        params = None
        start_frame = self.config.stable_offset_seconds * self.config.sample_rate
        for source in sources:
            with wave.open(str(source), "rb") as input_audio:
                if input_audio.getframerate() != self.config.sample_rate:
                    raise RuntimeError("分离结果采样率与实时会话不一致。")
                if input_audio.getnchannels() != self.config.channels:
                    raise RuntimeError("分离结果声道数与实时会话不一致。")
                if input_audio.getsampwidth() != 2 or input_audio.getcomptype() != "NONE":
                    raise RuntimeError("实时汇聚只支持未压缩 PCM16 模型输出。")
                if input_audio.getnframes() < start_frame + self.config.output_frames:
                    raise RuntimeError("分离结果长度不足，无法提取稳定区间。")
                input_audio.setpos(start_frame)
                chunks.append(input_audio.readframes(self.config.output_frames))
                if params is None:
                    params = input_audio.getparams()
        if params is None:
            raise RuntimeError("实时模型源分组不能为空。")
        frames = chunks[0] if len(chunks) == 1 else self._saturating_sum_pcm16(chunks)
        with wave.open(str(partial), "wb") as output_audio:
            output_audio.setparams(params)
            output_audio.writeframes(frames)
        os.replace(partial, destination)

    @staticmethod
    def _saturating_sum_pcm16(chunks: list[bytes]) -> bytes:
        if not chunks or any(len(chunk) != len(chunks[0]) for chunk in chunks):
            raise RuntimeError("待汇聚的模型源音轨长度不一致。")
        sample_count = len(chunks[0]) // 2
        accumulator = array("i", [0]) * sample_count
        for chunk in chunks:
            samples = array("h")
            samples.frombytes(chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            for index, sample in enumerate(samples):
                accumulator[index] += sample
        mixed = array(
            "h",
            (max(-32_768, min(32_767, sample)) for sample in accumulator),
        )
        if sys.byteorder != "little":
            mixed.byteswap()
        return mixed.tobytes()


class LiveSessionState(str, Enum):
    stopped = "stopped"
    warming = "warming"
    running = "running"
    error = "error"


class InvalidLiveTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveSnapshot:
    state: LiveSessionState = LiveSessionState.stopped
    process_id: int | None = None
    process_name: str | None = None
    buffered_seconds: float = 0.0
    latency_seconds: float | None = None
    last_sequence: int | None = None
    error: str | None = None


class LiveStateMachine:
    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot = LiveSnapshot()

    def snapshot(self) -> LiveSnapshot:
        with self._lock:
            return self._snapshot

    def start(self, process_id: int, process_name: str) -> None:
        if process_id <= 0 or not process_name.strip():
            raise ValueError("必须选择有效的音乐进程。")
        with self._lock:
            if self._snapshot.state is not LiveSessionState.stopped:
                raise InvalidLiveTransition("只有已停止的会话可以启动。")
            self._snapshot = LiveSnapshot(
                state=LiveSessionState.warming,
                process_id=process_id,
                process_name=process_name.strip(),
            )

    def mark_running(self, buffered_seconds: float) -> None:
        with self._lock:
            if self._snapshot.state is not LiveSessionState.warming:
                raise InvalidLiveTransition("只有预热中的会话可以进入运行状态。")
            self._snapshot = replace(
                self._snapshot,
                state=LiveSessionState.running,
                buffered_seconds=max(0.0, buffered_seconds),
                error=None,
            )

    def update_progress(
        self,
        sequence: int,
        latency_seconds: float,
        buffered_seconds: float,
    ) -> None:
        with self._lock:
            if self._snapshot.state is not LiveSessionState.running:
                raise InvalidLiveTransition("只有运行中的会话可以更新进度。")
            self._snapshot = replace(
                self._snapshot,
                last_sequence=sequence,
                latency_seconds=max(0.0, latency_seconds),
                buffered_seconds=max(0.0, buffered_seconds),
            )

    def fail(self, message: str) -> None:
        with self._lock:
            if self._snapshot.state is LiveSessionState.stopped:
                raise InvalidLiveTransition("已停止的会话不能直接进入错误状态。")
            self._snapshot = replace(
                self._snapshot,
                state=LiveSessionState.error,
                error=message.strip() or "未知实时处理错误",
            )

    def stop(self) -> None:
        with self._lock:
            self._snapshot = LiveSnapshot()
