from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .airplay_timeline import (
    AirPlayTrackSegment,
    CaptureAnnotation,
    try_load_capture_annotation,
)
from .core import LIVE_PROFILES, LiveProfile
from .inference_process import (
    InferenceDeadlineExceeded,
    InferenceWarmingUp,
    IsolatedPersistentSeparator,
)
from .live import LiveChunkProcessor, LiveConfig, PersistentSeparator, ProcessedChunk, discover_ready_chunks
from .song_cache import (
    Pcm16OverlapStitcher,
    SongCache,
    SongCacheBuilder,
    SongCacheProfile,
    SongCacheSlice,
    SongTrackMetadata,
)
from .track_cache import TrackCache, TrackCacheSpec, pcm_content_sha256


_result_pattern = re.compile(r"^result-(\d{8})\.json$")
_capture_artifact_pattern = re.compile(r"^capture-(\d{8})(?:\.wav|\.json)$")
_result_artifact_pattern = re.compile(
    r"^result-(\d{8})(?:\.json|-[a-z0-9_-]+\.wav)$",
    re.IGNORECASE,
)
_generated_artifact_pattern = re.compile(r"^capture-(\d{8})(?:\.wav|\.json|_.+)$")
_artifact_retention_sequences = 8
_default_cache_quota_bytes = 20 * 1024 * 1024 * 1024


class RealtimeBacklogError(RuntimeError):
    """GPU work is intentionally bypassed to protect the live playback buffer."""

    def __init__(self, message: str, fallback_reason: str = "realtime_backlog") -> None:
        super().__init__(message)
        self.fallback_reason = fallback_reason


@dataclass(frozen=True)
class _SongAssemblyHop:
    stream_start_frame: int
    stream_end_frame: int
    source_pcm: bytes
    stems: dict[str, bytes]


def last_published_sequence(outbox: str | Path) -> int:
    root = Path(outbox)
    if not root.is_dir():
        return 0
    sequences = []
    for path in root.iterdir():
        match = _result_pattern.fullmatch(path.name)
        if match:
            sequences.append(int(match.group(1)))
    return max(sequences, default=0)


def prune_live_artifacts(
    data_root: str | Path,
    safe_sequence: int,
    keep_sequences: int = _artifact_retention_sequences,
) -> dict[str, int]:
    """Remove only complete artifacts already copied into the playback queue."""
    if safe_sequence < 0:
        raise ValueError("安全清理序号不得为负数。")
    if keep_sequences < 1:
        raise ValueError("实时文件至少保留一个完整序号。")

    removed = {"inbox": 0, "outbox": 0, "work": 0, "failed": 0}
    cutoff = safe_sequence - keep_sequences
    if cutoff <= 0:
        return removed

    root = Path(data_root)
    patterns = {
        "inbox": _capture_artifact_pattern,
        "outbox": _result_artifact_pattern,
        "work": _generated_artifact_pattern,
        "failed": _generated_artifact_pattern,
    }
    for directory_name, pattern in patterns.items():
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            match = pattern.fullmatch(path.name)
            if not match or int(match.group(1)) > cutoff:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed[directory_name] += 1
    return removed


def _queued_playback_sequence(data_root: Path) -> int:
    status_path = data_root / "playback-status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8-sig"))
        return max(0, int(payload.get("queued_sequence", 0)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0


class LiveWorker:
    def __init__(
        self,
        data_root: str | Path,
        separator_factory: Callable[[LiveProfile], PersistentSeparator] | None = None,
        cache_quota_bytes: int = _default_cache_quota_bytes,
        inference_timeout_seconds: float = 5.5,
    ) -> None:
        if cache_quota_bytes < 0:
            raise ValueError("缓存容量上限不得为负数。")
        if inference_timeout_seconds <= 0.0:
            raise ValueError("实时推理硬时限必须为正数。")
        self.root = Path(data_root).resolve()
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.work = self.root / "work"
        self.failed = self.root / "failed"
        for directory in (self.inbox, self.outbox, self.work, self.failed):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = LiveConfig()
        self._separator_factory = separator_factory or self._default_separator
        self._processor: LiveChunkProcessor | None = None
        self._last_sequence = last_published_sequence(self.outbox)
        self._active_profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
        self._profile_command_sequence = 0
        self._session_active = False
        self._last_failure: dict | None = None
        self._cache = TrackCache(self.root / "cache")
        self._song_cache = SongCache(self.root / "song-cache")
        self._incomplete_song_builds_discarded = (
            self._song_cache.discard_incomplete_builds()
        )
        self._cache_quota_bytes = cache_quota_bytes
        self._inference_timeout_seconds = float(inference_timeout_seconds)
        self._last_cache_error: str | None = None
        self._processor_start_error: str | None = None
        self._cache_hits = 0
        self._cache_misses = 0
        self._fallback_windows = 0
        self._low_buffer_fallback_windows = 0
        self._warmup_windows = 0
        self._deadline_windows = 0
        self._max_processing_seconds = 0.0
        self._songs_cached = self._song_cache.complete_entry_count()
        self._song_builder: SongCacheBuilder | None = None
        self._song_builder_key: tuple[int, int] | None = None
        self._song_builder_revision: int | None = None
        self._song_builder_track_end_frame: int | None = None
        self._completed_song_keys: set[tuple[int, int]] = set()
        self._song_hop_history: list[_SongAssemblyHop] = []
        self._song_prefix_recoveries = 0
        self._song_prefix_recovery_frames = 0
        self._song_prefix_recovery_misses = 0
        self._last_song_prefix_miss: dict | None = None
        self._last_song_cache_outcome: dict | None = None
        self._song_stitcher: Pcm16OverlapStitcher | None = None
        self._song_stitch_signature: tuple | None = None
        self._song_last_sequence = 0
        self._timeline_epoch = 0
        self._timeline_last_start_frame: int | None = None
        self._last_status_payload: dict = {}
        self._last_written_status: dict | None = None
        self._status_write_error: str | None = None

    @property
    def active_profile(self) -> LiveProfile:
        return self._active_profile

    def _default_separator(self, profile: LiveProfile) -> IsolatedPersistentSeparator:
        return IsolatedPersistentSeparator(
            model_dir=self.root.parent / "models",
            work_dir=self.work,
            model_filename=profile.model_filename,
            inference_timeout_seconds=self._inference_timeout_seconds,
        )

    def _create_processor(self, separator=None) -> LiveChunkProcessor:
        resolved_separator = (
            self._separator_factory(self._active_profile)
            if separator is None
            else separator
        )
        return LiveChunkProcessor(
            self.config,
            self.outbox,
            resolved_separator,
            expected_stems=self._active_profile.stems,
            stem_sources=dict(
                zip(
                    self._active_profile.stems,
                    self._active_profile.source_groups,
                    strict=True,
                )
            ),
        )

    def _ensure_processor(self) -> None:
        if self._processor is None:
            self._processor = self._create_processor()
            self._processor_start_error = None

    def _prewarm_processor(self) -> None:
        try:
            self._ensure_processor()
        except Exception as exc:
            self._processor_start_error = str(exc).strip() or type(exc).__name__

    def _discard_processor(self) -> None:
        processor = self._processor
        self._processor = None
        if processor is not None:
            processor.close()

    def _sync_requested_profile(self) -> None:
        command_path = self.root / "command.json"
        if not command_path.is_file():
            return
        payload = json.loads(command_path.read_text(encoding="utf-8-sig"))
        sequence = int(payload.get("sequence", 0))
        if sequence <= self._profile_command_sequence:
            return
        action = payload.get("action")
        if action == "stop":
            self._session_active = False
            self._reset_song_assembly()
            self._discard_processor()
            self._last_status_payload.update(
                {
                    "state": "waiting",
                    "profile_name": self._active_profile.name,
                    "error": None,
                    "recovering": False,
                }
            )
            self._profile_command_sequence = sequence
            return
        if action not in {"start", "start_airplay"}:
            self._profile_command_sequence = sequence
            return
        profile_name = str(payload.get("profile_name", "人声 / 伴奏 · 高质量"))
        if profile_name not in LIVE_PROFILES:
            raise ValueError("实时控制命令包含未知分离模式。")
        requested = LIVE_PROFILES[profile_name]
        if requested != self._active_profile:
            self._reset_song_assembly()
            reusable_separator = None
            if (
                self._processor is not None
                and requested.model_filename == self._active_profile.model_filename
            ):
                reusable_separator = self._processor.detach_separator()
            self._discard_processor()
            self._active_profile = requested
            if reusable_separator is not None:
                self._processor = self._create_processor(reusable_separator)
        self._session_active = True
        self._last_status_payload.update(
            {
                "state": "waiting",
                "profile_name": requested.name,
                "error": None,
                "recovering": False,
            }
        )
        self._prewarm_processor()
        self._profile_command_sequence = sequence

    def _publish_failure_fallback(
        self,
        chunk,
        message: str,
        processing_seconds: float,
        fallback_reason: str,
    ) -> ProcessedChunk:
        preferred_stem = (
            "other"
            if "other" in self._active_profile.stems
            else "instrumental"
            if "instrumental" in self._active_profile.stems
            else self._active_profile.stems[-1]
        )
        fallback_stem = preferred_stem
        fallback_output_gain = 1.0
        try:
            playback = json.loads(
                (self.root / "playback-status.json").read_text(encoding="utf-8-sig")
            )
            raw_gains = playback["gains"]
            if not isinstance(raw_gains, dict):
                raise TypeError("播放状态中的音轨增益无效。")
            gains: dict[str, float] = {}
            for stem in self._active_profile.stems:
                gain = float(raw_gains[stem])
                if not math.isfinite(gain) or gain < 0.0 or gain > 1.0:
                    raise ValueError("播放状态中的音轨增益超出范围。")
                gains[stem] = gain
            fallback_stem = max(
                self._active_profile.stems,
                key=lambda stem: (gains[stem], stem == preferred_stem),
            )
            fallback_output_gain = gains[fallback_stem]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        source_pcm = self._read_capture_pcm(
            chunk,
            self.config.stable_offset_seconds * self.config.sample_rate,
            self.config.output_frames,
        )
        silence = b"\x00" * len(source_pcm)
        published: dict[str, str] = {}
        for stem in self._active_profile.stems:
            filename = f"result-{chunk.sequence:08d}-{stem}.wav"
            destination = self.outbox / filename
            partial = destination.with_suffix(".wav.part")
            with wave.open(str(partial), "wb") as audio:
                audio.setnchannels(self.config.channels)
                audio.setsampwidth(2)
                audio.setframerate(self.config.sample_rate)
                audio.writeframes(source_pcm if stem == fallback_stem else silence)
            os.replace(partial, destination)
            published[stem] = filename

        latency = self.config.window_seconds + max(
            0.0,
            time.time() - chunk.path.stat().st_mtime,
        )
        manifest = self.outbox / f"result-{chunk.sequence:08d}.json"
        partial = manifest.with_suffix(".json.part")
        partial.write_text(
            json.dumps(
                {
                    "version": 2,
                    "sequence": chunk.sequence,
                    "sample_rate": self.config.sample_rate,
                    "channels": self.config.channels,
                    "window_seconds": self.config.window_seconds,
                    "hop_seconds": self.config.hop_seconds,
                    "stable_offset_seconds": self.config.stable_offset_seconds,
                    "overlap_frames": self.config.overlap_frames,
                    "processing_seconds": round(max(0.0, processing_seconds), 3),
                    "latency_seconds": round(latency, 3),
                    "cache_hit": False,
                    "cache_scope": "fallback",
                    "fallback_audio": True,
                    "fallback_reason": fallback_reason,
                    "fallback_stem": fallback_stem,
                    "fallback_output_gain": fallback_output_gain,
                    "error": message,
                    "stems": published,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(partial, manifest)
        self._fallback_windows += 1
        return ProcessedChunk(chunk.sequence, manifest, latency)

    def _quarantine(
        self,
        chunk,
        error: Exception,
        processing_seconds: float,
        fallback_reason: str = "processing_failure",
    ) -> ProcessedChunk | None:
        message = str(error).strip() or type(error).__name__
        fallback: ProcessedChunk | None = None
        try:
            fallback = self._publish_failure_fallback(
                chunk,
                message,
                processing_seconds,
                fallback_reason,
            )
        except Exception as fallback_error:
            fallback_message = str(fallback_error).strip() or type(fallback_error).__name__
            message = f"{message}；原声降级也失败：{fallback_message}"
            for stem in self._active_profile.stems:
                (self.outbox / f"result-{chunk.sequence:08d}-{stem}.wav").unlink(
                    missing_ok=True
                )
            manifest = self.outbox / f"result-{chunk.sequence:08d}.json"
            partial = manifest.with_suffix(".json.part")
            partial.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sequence": chunk.sequence,
                        "error": message,
                        "fallback_audio": False,
                        "stems": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(partial, manifest)
        destination = self.failed / chunk.path.name
        if chunk.path.is_file():
            os.replace(chunk.path, destination)
        annotation = chunk.path.with_suffix(".json")
        if annotation.is_file():
            os.replace(annotation, self.failed / annotation.name)
        prefix = f"capture-{chunk.sequence:08d}_"
        for generated in self.work.glob(f"{prefix}*"):
            if generated.is_file():
                os.replace(generated, self.failed / generated.name)
        self._last_sequence = chunk.sequence
        self._last_failure = {"sequence": chunk.sequence, "error": message}
        return fallback

    def _continuity_reserve_seconds(self) -> float:
        return max(
            self._inference_timeout_seconds + 1.0,
            float(self.config.hop_seconds) + 1.0,
        )

    def _continuity_fallback_reason(self, ready_count: int) -> str | None:
        if ready_count <= 0:
            return None
        try:
            playback = json.loads(
                (self.root / "playback-status.json").read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(playback, dict) or playback.get("state") != "playing":
            return None
        if ready_count >= 2:
            return "realtime_backlog"
        try:
            buffered_seconds = float(playback.get("buffered_seconds"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(buffered_seconds) or buffered_seconds < 0.0:
            return None
        if buffered_seconds <= self._continuity_reserve_seconds():
            return "low_buffer_reserve"
        return None

    def _cache_spec(self, chunk) -> TrackCacheSpec:
        return TrackCacheSpec(
            audio_identity=pcm_content_sha256(chunk.path),
            profile_name=self._active_profile.name,
            model_filename=self._active_profile.model_filename,
            stems=self._active_profile.stems,
            sample_rate=self.config.sample_rate,
            channels=self.config.channels,
            bits_per_sample=16,
            window_seconds=self.config.window_seconds,
            hop_seconds=self.config.hop_seconds,
            stable_offset_seconds=self.config.stable_offset_seconds,
            overlap_frames=self.config.overlap_frames,
        )

    def _song_profile(self) -> SongCacheProfile:
        return SongCacheProfile(
            profile_name=self._active_profile.name,
            model_filename=self._active_profile.model_filename,
            stems=self._active_profile.stems,
            sample_rate=self.config.sample_rate,
            channels=self.config.channels,
            bits_per_sample=16,
            window_seconds=self.config.window_seconds,
            hop_seconds=self.config.hop_seconds,
            stable_offset_seconds=self.config.stable_offset_seconds,
            overlap_frames=self.config.overlap_frames,
        )

    def _load_annotation(self, chunk) -> CaptureAnnotation | None:
        return try_load_capture_annotation(
            chunk.path.with_suffix(".json"),
            expected_sequence=chunk.sequence,
            expected_sample_rate=self.config.sample_rate,
            expected_window_frames=self.config.window_frames,
        )

    def _read_capture_pcm(self, chunk, offset_frames: int, frame_count: int) -> bytes:
        with wave.open(str(chunk.path), "rb") as audio:
            if (
                audio.getframerate() != self.config.sample_rate
                or audio.getnchannels() != self.config.channels
                or audio.getsampwidth() != 2
                or offset_frames < 0
                or offset_frames + frame_count > audio.getnframes()
            ):
                raise RuntimeError("实时捕获 WAV 几何参数无效。")
            audio.setpos(offset_frames)
            pcm = audio.readframes(frame_count)
        if len(pcm) != frame_count * self.config.channels * 2:
            raise RuntimeError("实时捕获 WAV 读取不完整。")
        return pcm

    def _song_cache_hit(
        self,
        chunk,
        annotation: CaptureAnnotation | None,
    ) -> ProcessedChunk | None:
        if annotation is None:
            return None
        try:
            offset_frames = self.config.stable_offset_seconds * self.config.sample_rate
            segments = annotation.output_segments(
                offset_frames=offset_frames,
                frame_count=self.config.output_frames,
            )
            output_start = annotation.stream_start_frame + offset_frames
            output_end = output_start + self.config.output_frames
            if (
                not segments
                or segments[0].stream_start_frame != output_start
                or segments[-1].stream_end_frame != output_end
                or any(
                    previous.stream_end_frame != current.stream_start_frame
                    for previous, current in zip(segments, segments[1:])
                )
            ):
                return None
            groups: list[list[AirPlayTrackSegment]] = []
            for segment in segments:
                if segment.revision <= 0:
                    return None
                previous = groups[-1][-1] if groups else None
                if (
                    previous is not None
                    and previous.revision == segment.revision
                    and previous.track_duration_frame == segment.track_duration_frame
                    and previous.track_end_frame == segment.track_start_frame
                ):
                    groups[-1].append(segment)
                else:
                    groups.append([segment])

            cache_slices: list[SongCacheSlice] = []
            profile = self._song_profile()
            for group in groups:
                first = group[0]
                last = group[-1]
                group_frames = last.stream_end_frame - first.stream_start_frame
                if group_frames <= 0:
                    return None
                metadata_segment = next(
                    (
                        segment
                        for segment in reversed(group)
                        if segment.title.strip() or segment.artist.strip()
                    ),
                    None,
                )
                if metadata_segment is None:
                    return None
                metadata = SongTrackMetadata(
                    title=metadata_segment.title,
                    artist=metadata_segment.artist,
                    album=metadata_segment.album,
                    duration_frames=metadata_segment.track_duration_frame,
                    sample_rate=self.config.sample_rate,
                )
                source_pcm = self._read_capture_pcm(
                    chunk,
                    first.stream_start_frame - annotation.stream_start_frame,
                    group_frames,
                )
                matched = None
                for entry in self._song_cache.lookup(
                    metadata,
                    profile,
                    allow_duration_mismatch=True,
                ):
                    aligned = entry.align_source(
                        source_pcm,
                        approximate_track_start_frame=first.track_start_frame,
                    )
                    if aligned is not None:
                        matched = SongCacheSlice(
                            entry,
                            cache_start_frame=aligned,
                            frame_count=group_frames,
                        )
                        break
                if matched is None:
                    return None
                cache_slices.append(matched)

            latency = self.config.window_seconds + max(
                0.0,
                time.time() - chunk.path.stat().st_mtime,
            )
            if len(cache_slices) == 1:
                item = cache_slices[0]
                manifest = item.entry.publish_range(
                    cache_start_frame=item.cache_start_frame,
                    frame_count=item.frame_count,
                    outbox=self.outbox,
                    sequence=chunk.sequence,
                    latency_seconds=latency,
                )
            else:
                manifest = self._song_cache.publish_composite(
                    slices=tuple(cache_slices),
                    outbox=self.outbox,
                    sequence=chunk.sequence,
                    latency_seconds=latency,
                )
            self._cache_hits += 1
            self._last_cache_error = None
            return ProcessedChunk(chunk.sequence, manifest, latency)
        except Exception as exc:
            self._last_cache_error = str(exc).strip() or type(exc).__name__
        return None

    def _annotation_supports_song_cache(
        self,
        annotation: CaptureAnnotation | None,
    ) -> bool:
        if annotation is None:
            return False
        offset_frames = self.config.stable_offset_seconds * self.config.sample_rate
        segments = annotation.output_segments(
            offset_frames=offset_frames,
            frame_count=self.config.hop_frames,
        )
        output_start = annotation.stream_start_frame + offset_frames
        return bool(
            segments
            and segments[0].stream_start_frame == output_start
            and segments[-1].stream_end_frame
            == output_start + self.config.hop_frames
            and all(segment.revision > 0 for segment in segments)
            and all(
                segment.title.strip() or segment.artist.strip()
                for segment in segments
            )
        )

    def _discard_song_builder(self) -> None:
        if self._song_builder is not None:
            self._song_builder.discard()
        self._song_builder = None
        self._song_builder_key = None
        self._song_builder_revision = None
        self._song_builder_track_end_frame = None

    def _finalize_song_builder(self) -> None:
        builder = self._song_builder
        key = self._song_builder_key
        revision = self._song_builder_revision
        self._song_builder = None
        self._song_builder_key = None
        self._song_builder_revision = None
        self._song_builder_track_end_frame = None
        if builder is None or key is None:
            return
        self._completed_song_keys.add(key)
        metadata = builder.metadata
        diagnostic = {
            "timeline_epoch": key[0],
            "track_revision": revision or 0,
            "track_start_rtp": key[1],
            "title": metadata.title if metadata is not None else "",
            "first_track_start_frame": builder.first_track_start_frame,
            "frame_count": builder.frame_count,
            "duration_frames": metadata.duration_frames if metadata is not None else None,
        }
        rejection_reason = "publish_rejected"
        if metadata is None:
            rejection_reason = "missing_metadata"
        elif builder.first_track_start_frame is None:
            rejection_reason = "missing_track_start"
        elif builder.first_track_start_frame > builder.profile.sample_rate * 2:
            rejection_reason = "missing_song_prefix"
        elif builder.frame_count < metadata.duration_frames:
            rejection_reason = "incomplete_song"
        elif metadata.sample_rate != builder.profile.sample_rate:
            rejection_reason = "sample_rate_mismatch"
        try:
            entry = builder.finalize()
            if entry is not None:
                self._song_cache.prune_to_quota(
                    self._cache_quota_bytes * 3 // 4
                )
                self._songs_cached = self._song_cache.complete_entry_count()
                self._last_song_cache_outcome = {
                    **diagnostic,
                    "state": "stored",
                    "reason": None,
                    "cache_key": entry.cache_key,
                }
            else:
                self._last_song_cache_outcome = {
                    **diagnostic,
                    "state": "discarded",
                    "reason": rejection_reason,
                    "cache_key": None,
                }
        except Exception as exc:
            builder.discard()
            self._last_cache_error = str(exc).strip() or type(exc).__name__
            self._last_song_cache_outcome = {
                **diagnostic,
                "state": "error",
                "reason": self._last_cache_error,
                "cache_key": None,
            }

    def _reset_song_assembly(self) -> None:
        self._discard_song_builder()
        if self._song_stitcher is not None:
            self._song_stitcher.reset()
        self._song_stitcher = None
        self._song_stitch_signature = None
        self._song_last_sequence = 0
        self._song_hop_history.clear()
        self._timeline_last_start_frame = None
        self._timeline_epoch += 1
        self._completed_song_keys.clear()

    def _ensure_song_stitcher(self) -> Pcm16OverlapStitcher:
        signature = (
            self._active_profile.stems,
            self.config.channels,
            self.config.hop_frames,
            self.config.overlap_frames,
        )
        if self._song_stitcher is None or self._song_stitch_signature != signature:
            self._discard_song_builder()
            self._song_hop_history.clear()
            self._song_stitcher = Pcm16OverlapStitcher(
                stems=self._active_profile.stems,
                channels=self.config.channels,
                hop_frames=self.config.hop_frames,
                overlap_frames=self.config.overlap_frames,
            )
            self._song_stitch_signature = signature
            self._song_last_sequence = 0
        return self._song_stitcher

    def _remember_song_hop(self, hop: _SongAssemblyHop) -> None:
        self._song_hop_history.append(hop)
        del self._song_hop_history[:-3]

    def _song_history_slice(
        self,
        stream_start_frame: int,
        stream_end_frame: int,
        current_hop: _SongAssemblyHop | None = None,
    ) -> tuple[bytes, dict[str, bytes]] | None:
        if stream_start_frame < 0 or stream_end_frame <= stream_start_frame:
            return None
        bytes_per_frame = self.config.channels * 2
        cursor = stream_start_frame
        source_parts: list[bytes] = []
        stem_parts: dict[str, list[bytes]] = {
            stem: [] for stem in self._active_profile.stems
        }
        available_hops = [*self._song_hop_history]
        if current_hop is not None:
            available_hops.append(current_hop)
        for hop in available_hops:
            if hop.stream_end_frame <= cursor:
                continue
            if hop.stream_start_frame > cursor:
                return None
            local_start = cursor - hop.stream_start_frame
            local_end = min(stream_end_frame, hop.stream_end_frame) - hop.stream_start_frame
            if local_end <= local_start:
                continue
            byte_start = local_start * bytes_per_frame
            byte_end = local_end * bytes_per_frame
            source_parts.append(hop.source_pcm[byte_start:byte_end])
            for stem in self._active_profile.stems:
                stem_parts[stem].append(hop.stems[stem][byte_start:byte_end])
            cursor = hop.stream_start_frame + local_end
            if cursor == stream_end_frame:
                return (
                    b"".join(source_parts),
                    {stem: b"".join(parts) for stem, parts in stem_parts.items()},
                )
        return None

    def _read_result_pcm(self, result: ProcessedChunk) -> dict[str, bytes]:
        payload = json.loads(result.manifest.read_text(encoding="utf-8-sig"))
        published = payload["stems"]
        chunks: dict[str, bytes] = {}
        for stem in self._active_profile.stems:
            filename = str(published[stem])
            if Path(filename).name != filename:
                raise RuntimeError("实时结果清单包含不安全路径。")
            path = self.outbox / filename
            with wave.open(str(path), "rb") as audio:
                if (
                    audio.getframerate() != self.config.sample_rate
                    or audio.getnchannels() != self.config.channels
                    or audio.getsampwidth() != 2
                    or audio.getnframes() != self.config.output_frames
                ):
                    raise RuntimeError("实时分轨结果无法写入歌曲缓存。")
                chunks[stem] = audio.readframes(audio.getnframes())
        return chunks

    def _update_song_assembly(
        self,
        chunk,
        result: ProcessedChunk,
        annotation: CaptureAnnotation | None,
    ) -> None:
        if annotation is None:
            self._discard_song_builder()
            if self._song_stitcher is not None:
                self._song_stitcher.reset()
            self._song_hop_history.clear()
            self._song_last_sequence = chunk.sequence
            return
        try:
            if (
                self._timeline_last_start_frame is not None
                and annotation.stream_start_frame < self._timeline_last_start_frame
            ):
                self._finalize_song_builder()
                if self._song_stitcher is not None:
                    self._song_stitcher.reset()
                self._song_last_sequence = 0
                self._song_hop_history.clear()
                self._timeline_epoch += 1
                self._completed_song_keys.clear()
            self._timeline_last_start_frame = annotation.stream_start_frame

            stitcher = self._ensure_song_stitcher()
            if self._song_last_sequence and chunk.sequence != self._song_last_sequence + 1:
                self._discard_song_builder()
                stitcher.reset()
                self._song_hop_history.clear()
            stitched = stitcher.push(self._read_result_pcm(result))
            self._song_last_sequence = chunk.sequence

            offset_frames = self.config.stable_offset_seconds * self.config.sample_rate
            source_hop = self._read_capture_pcm(
                chunk,
                offset_frames,
                self.config.hop_frames,
            )
            output_start = annotation.stream_start_frame + offset_frames
            current_hop = _SongAssemblyHop(
                stream_start_frame=output_start,
                stream_end_frame=output_start + self.config.hop_frames,
                source_pcm=source_hop,
                stems={stem: stitched[stem] for stem in self._active_profile.stems},
            )
            segments = annotation.output_segments(
                offset_frames=offset_frames,
                frame_count=self.config.hop_frames,
            )
            if not segments:
                self._discard_song_builder()
                self._remember_song_hop(current_hop)
                return

            bytes_per_frame = self.config.channels * 2
            for segment in segments:
                if segment.revision == 0:
                    self._discard_song_builder()
                    continue
                key = (self._timeline_epoch, segment.start_rtp)
                metadata = SongTrackMetadata(
                    title=segment.title,
                    artist=segment.artist,
                    album=segment.album,
                    duration_frames=segment.track_duration_frame,
                    sample_rate=self.config.sample_rate,
                )
                resync_tolerance_frames = self.config.sample_rate // 2
                current_metadata = (
                    self._song_builder.metadata
                    if self._song_builder_key == key
                    and self._song_builder is not None
                    else None
                )
                duration_delta = (
                    metadata.duration_frames - current_metadata.duration_frames
                    if current_metadata is not None
                    else 0
                )
                track_delta = (
                    segment.track_start_frame - self._song_builder_track_end_frame
                    if self._song_builder_key == key
                    and self._song_builder_track_end_frame is not None
                    else 0
                )
                revision_changed = (
                    self._song_builder_revision is not None
                    and segment.revision != self._song_builder_revision
                )
                tolerated_progress_resync = (
                    revision_changed
                    and abs(track_delta) <= resync_tolerance_frames
                )
                duration_changed_too_much = (
                    current_metadata is not None
                    and abs(duration_delta) > resync_tolerance_frames
                )
                if self._song_builder_key == key and (
                    (track_delta != 0 and not tolerated_progress_resync)
                    or duration_changed_too_much
                ):
                    self._finalize_song_builder()
                    self._timeline_epoch += 1
                    self._completed_song_keys.clear()
                    key = (self._timeline_epoch, segment.start_rtp)
                    current_metadata = None
                elif current_metadata is not None:
                    metadata_refresh_allowed = (
                        self._song_builder is not None
                        and self._song_builder.frame_count
                        <= self.config.sample_rate * 2
                    )
                    metadata = SongTrackMetadata(
                        title=(
                            segment.title
                            if metadata_refresh_allowed
                            else current_metadata.title
                        ),
                        artist=(
                            segment.artist
                            if metadata_refresh_allowed
                            else current_metadata.artist
                        ),
                        album=(
                            segment.album
                            if metadata_refresh_allowed
                            else current_metadata.album
                        ),
                        duration_frames=current_metadata.duration_frames,
                        sample_rate=self.config.sample_rate,
                    )
                if self._song_builder_key != key:
                    self._finalize_song_builder()
                    if key in self._completed_song_keys:
                        continue
                    token = (
                        f"{self._timeline_epoch}:{segment.start_rtp}:"
                        f"{segment.revision}:"
                        f"{chunk.sequence}:{segment.stream_start_frame}"
                    )
                    self._song_builder = self._song_cache.start_build(
                        token,
                        self._song_profile(),
                    )
                    self._song_builder_key = key
                    self._song_builder_revision = segment.revision
                    track_stream_start = (
                        segment.stream_start_frame - segment.track_start_frame
                    )
                    recovered = self._song_history_slice(
                        track_stream_start,
                        segment.stream_start_frame,
                        current_hop,
                    )
                    if segment.track_start_frame > 0 and recovered is None:
                        self._song_prefix_recovery_misses += 1
                        self._last_song_prefix_miss = {
                            "track_revision": segment.revision,
                            "title": segment.title,
                            "required_stream_start_frame": track_stream_start,
                            "required_stream_end_frame": segment.stream_start_frame,
                            "track_start_frame": segment.track_start_frame,
                            "history": [
                                [hop.stream_start_frame, hop.stream_end_frame]
                                for hop in [*self._song_hop_history, current_hop]
                            ],
                        }
                    if recovered is not None:
                        recovered_source, recovered_stems = recovered
                        recovered_frames = len(recovered_source) // bytes_per_frame
                        self._song_prefix_recoveries += 1
                        self._song_prefix_recovery_frames += recovered_frames
                        assert self._song_builder is not None
                        self._song_builder.append(
                            stream_start_frame=track_stream_start,
                            track_start_frame=0,
                            metadata=metadata,
                            source_pcm=recovered_source,
                            stems=recovered_stems,
                        )
                        self._song_builder_track_end_frame = (
                            segment.track_start_frame
                        )
                local_start = segment.stream_start_frame - output_start
                local_end = segment.stream_end_frame - output_start
                byte_start = local_start * bytes_per_frame
                byte_end = local_end * bytes_per_frame
                assert self._song_builder is not None
                self._song_builder.append(
                    stream_start_frame=segment.stream_start_frame,
                    track_start_frame=segment.track_start_frame,
                    metadata=metadata,
                    source_pcm=source_hop[byte_start:byte_end],
                    stems={
                        stem: stitched[stem][byte_start:byte_end]
                        for stem in self._active_profile.stems
                    },
                )
                self._song_builder_track_end_frame = segment.track_end_frame
                self._song_builder_revision = segment.revision
                if self._song_builder.frame_count >= metadata.duration_frames:
                    self._finalize_song_builder()
            self._remember_song_hop(current_hop)
        except Exception as exc:
            self._discard_song_builder()
            if self._song_stitcher is not None:
                self._song_stitcher.reset()
            self._song_hop_history.clear()
            self._last_cache_error = str(exc).strip() or type(exc).__name__

    def _cache_hit(self, chunk, spec: TrackCacheSpec) -> ProcessedChunk | None:
        entry = self._cache.lookup(spec)
        if entry is None:
            return None
        self._cache_hits += 1
        latency = self.config.window_seconds + max(
            0.0,
            time.time() - chunk.path.stat().st_mtime,
        )
        manifest = entry.publish_chunk(0, self.outbox, chunk.sequence)
        return ProcessedChunk(chunk.sequence, manifest, latency)

    def _cache_result(
        self,
        chunk,
        result: ProcessedChunk,
        spec: TrackCacheSpec,
        annotation: CaptureAnnotation | None,
    ) -> None:
        try:
            if self._annotation_supports_song_cache(annotation):
                return
            payload = json.loads(result.manifest.read_text(encoding="utf-8-sig"))
            published = payload["stems"]
            stem_paths = {
                stem: self.outbox / str(published[stem])
                for stem in self._active_profile.stems
            }
            metadata: dict[str, object] = {"capture_sequence": chunk.sequence}
            if annotation is not None:
                metadata["airplay"] = annotation.to_dict()
                metadata["output_segments"] = [
                    segment.to_dict()
                    for segment in annotation.output_segments(
                        offset_frames=(
                            self.config.stable_offset_seconds * self.config.sample_rate
                        ),
                        frame_count=self.config.output_frames,
                    )
                ]
            self._cache.store_chunk(spec, 0, stem_paths)
            self._cache.finalize(spec, chunk_count=1, metadata=metadata)
            self._cache.prune_to_quota(self._cache_quota_bytes // 4)
            self._last_cache_error = None
        except Exception as exc:
            # Playback has already been published. Cache maintenance must never
            # turn a valid real-time result into a dropped audio window.
            self._last_cache_error = str(exc).strip() or type(exc).__name__

    def process_available(self, max_chunks: int = 1) -> list[ProcessedChunk]:
        self._sync_requested_profile()
        if self._session_active:
            self._prewarm_processor()
        queued_sequence = _queued_playback_sequence(self.root)
        if queued_sequence:
            prune_live_artifacts(self.root, queued_sequence)
        available = discover_ready_chunks(self.inbox, self._last_sequence)
        ready = available[:max_chunks]
        if not ready:
            return []
        results = []
        for index, chunk in enumerate(ready):
            started = time.perf_counter()
            # Cache errors describe the current processing window. AirPlay can
            # legitimately publish progress before its title/artist metadata;
            # do not leave that transient startup error visible after a later
            # window has recovered. Any failure below records a fresh error.
            self._last_cache_error = None
            try:
                annotation = self._load_annotation(chunk)
                result = self._song_cache_hit(chunk, annotation)
                used_song_cache = result is not None
                if result is None:
                    spec = self._cache_spec(chunk)
                    result = self._cache_hit(chunk, spec)
                    if result is None:
                        self._cache_misses += 1
                        fallback_reason = self._continuity_fallback_reason(
                            len(available) - index
                        )
                        if fallback_reason == "realtime_backlog":
                            raise RealtimeBacklogError(
                                "实时积压已超过一个窗口，切换原声保底以保护播放缓冲。"
                            )
                        if fallback_reason == "low_buffer_reserve":
                            self._low_buffer_fallback_windows += 1
                            raise RealtimeBacklogError(
                                "可播放缓存已低于 GPU 推理安全余量，立即切换原声保底以避免断音。",
                                fallback_reason="low_buffer_reserve",
                            )
                        self._ensure_processor()
                        result = self._processor.process(chunk)
                        self._cache_result(chunk, result, spec, annotation)
                if not used_song_cache:
                    self._update_song_assembly(chunk, result, annotation)
            except Exception as exc:
                processing_seconds = time.perf_counter() - started
                self._max_processing_seconds = max(
                    self._max_processing_seconds,
                    processing_seconds,
                )
                if isinstance(exc, InferenceWarmingUp):
                    self._warmup_windows += 1
                elif isinstance(exc, InferenceDeadlineExceeded):
                    self._deadline_windows += 1
                fallback = self._quarantine(
                    chunk,
                    exc,
                    processing_seconds,
                    (
                        exc.fallback_reason
                        if isinstance(exc, RealtimeBacklogError)
                        else "model_warmup"
                        if isinstance(exc, InferenceWarmingUp)
                        else "inference_deadline"
                        if isinstance(exc, InferenceDeadlineExceeded)
                        else "processing_failure"
                    ),
                )
                # A separator can retain partial per-file state after an exception.
                # Recreate it for the next window so one bad output cannot poison the session.
                if not isinstance(exc, InferenceWarmingUp):
                    self._discard_processor()
                self._discard_song_builder()
                if self._song_stitcher is not None:
                    self._song_stitcher.reset()
                if fallback is not None:
                    results.append(fallback)
            else:
                self._last_sequence = result.sequence
                self._last_failure = None
                results.append(result)
                try:
                    payload = json.loads(result.manifest.read_text(encoding="utf-8-sig"))
                    processing_seconds = max(0.0, float(payload.get("processing_seconds", 0.0)))
                    if math.isfinite(processing_seconds):
                        self._max_processing_seconds = max(
                            self._max_processing_seconds,
                            processing_seconds,
                        )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pass
        return results

    def run(self, stop_event: threading.Event, poll_seconds: float = 0.25) -> None:
        self._write_status(
            {
                "state": "waiting",
                "last_sequence": self._last_sequence,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "fallback_windows": self._fallback_windows,
                "songs_cached": self._songs_cached,
            }
        )
        while not stop_event.is_set():
            try:
                results = self.process_available()
                if results:
                    latest = results[-1]
                    latest_payload = json.loads(
                        latest.manifest.read_text(encoding="utf-8-sig")
                    )
                    fallback_audio = latest_payload.get("fallback_audio") is True
                    self._write_status(
                        {
                            "state": "degraded" if fallback_audio else "running",
                            "last_sequence": latest.sequence,
                            "latency_seconds": round(latest.latency_seconds, 3),
                            "profile_name": self._active_profile.name,
                            "cache_hit": latest_payload.get("cache_hit") is True,
                            "cache_hits": self._cache_hits,
                            "cache_misses": self._cache_misses,
                            "fallback_audio": fallback_audio,
                            "fallback_windows": self._fallback_windows,
                            "recovering": fallback_audio,
                            "error": (
                                latest_payload.get("error")
                                if fallback_audio
                                else None
                            ),
                            "songs_cached": self._songs_cached,
                            "cache_error": self._last_cache_error,
                        }
                    )
                elif self._last_failure:
                    self._write_status(
                        {
                            "state": "degraded",
                            "last_sequence": self._last_sequence,
                            "failed_sequence": self._last_failure["sequence"],
                            "error": self._last_failure["error"],
                            "recovering": True,
                            "profile_name": self._active_profile.name,
                            "cache_hits": self._cache_hits,
                            "cache_misses": self._cache_misses,
                            "fallback_audio": False,
                            "fallback_windows": self._fallback_windows,
                            "songs_cached": self._songs_cached,
                            "cache_error": self._last_cache_error,
                        }
                    )
                else:
                    # Refresh model readiness even before a phone has produced
                    # its first capture window.
                    self._write_status({})
            except Exception as exc:
                self._write_status(
                    {
                        "state": "error",
                        "error": str(exc),
                        "last_sequence": self._last_sequence,
                        "cache_hits": self._cache_hits,
                        "cache_misses": self._cache_misses,
                        "fallback_audio": False,
                        "fallback_windows": self._fallback_windows,
                        "songs_cached": self._songs_cached,
                        "cache_error": self._last_cache_error,
                    }
                )
                time.sleep(1.0)
            stop_event.wait(poll_seconds)
        self._discard_processor()

    def _write_status(self, payload: dict) -> None:
        self._last_status_payload.update(payload)
        observability: dict[str, object] = {
            "model_state": (
                "error" if self._processor_start_error else "stopped"
            ),
            "inference_process_pid": None,
            "inference_timeout_seconds": self._inference_timeout_seconds,
            "model_warmup_seconds": None,
            "inference_error": self._processor_start_error,
            "warmup_windows": self._warmup_windows,
            "deadline_windows": self._deadline_windows,
            "low_buffer_fallback_windows": self._low_buffer_fallback_windows,
            "continuity_reserve_seconds": round(
                self._continuity_reserve_seconds(),
                3,
            ),
            "max_processing_seconds": round(self._max_processing_seconds, 3),
            "orphan_song_builds_removed": self._incomplete_song_builds_discarded,
            "song_prefix_recoveries": self._song_prefix_recoveries,
            "song_prefix_recovery_frames": self._song_prefix_recovery_frames,
            "song_prefix_recovery_misses": self._song_prefix_recovery_misses,
            "last_song_prefix_miss": self._last_song_prefix_miss,
            "last_song_cache_outcome": self._last_song_cache_outcome,
            "song_builder_track_revision": self._song_builder_revision,
            "song_builder_start_rtp": (
                self._song_builder_key[1]
                if self._song_builder_key is not None
                else None
            ),
            "song_builder_first_track_start_frame": (
                self._song_builder.first_track_start_frame
                if self._song_builder is not None
                else None
            ),
            "song_builder_frame_count": (
                self._song_builder.frame_count
                if self._song_builder is not None
                else 0
            ),
            "song_builder_duration_frames": (
                self._song_builder.metadata.duration_frames
                if self._song_builder is not None
                and self._song_builder.metadata is not None
                else None
            ),
        }
        if self._processor is not None:
            status_provider = getattr(self._processor.separator, "status_snapshot", None)
            if callable(status_provider):
                try:
                    separator_status = status_provider()
                    if isinstance(separator_status, dict):
                        observability.update(separator_status)
                except Exception as exc:
                    observability["model_state"] = "error"
                    observability["inference_error"] = str(exc).strip() or type(exc).__name__
            else:
                observability["model_state"] = "ready"
        complete_payload = {**self._last_status_payload, **observability}
        if complete_payload == self._last_written_status:
            return
        destination = self.root / "gpu-status.json"
        partial = destination.with_suffix(".json.part")
        try:
            partial.write_text(
                json.dumps(complete_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            replaced = False
            for attempt in range(5):
                try:
                    os.replace(partial, destination)
                    replaced = True
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(0.01)
            if not replaced:
                raise PermissionError("实时状态文件持续被占用。")
        except OSError as exc:
            # Status telemetry is advisory. A Windows reader briefly holding a
            # bind-mounted file must never stop capture, inference, or playback.
            self._status_write_error = str(exc).strip() or type(exc).__name__
            partial.unlink(missing_ok=True)
            return
        self._status_write_error = None
        self._last_written_status = complete_payload


def start_live_worker(data_root: str | Path) -> tuple[threading.Thread, threading.Event]:
    worker = LiveWorker(data_root)
    stop_event = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop_event,), name="live-gpu-worker", daemon=True)
    thread.start()
    return thread, stop_event
