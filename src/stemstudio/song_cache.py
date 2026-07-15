from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Mapping

from .track_cache import pcm_content_sha256


_SCHEMA_VERSION = 1
_STEM_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALGORITHM_VERSION = "continuous-stem-cache-v1"
_NORMALIZED_ALIGNMENT_MIN_CORRELATION = 0.985
_NORMALIZED_ALIGNMENT_MIN_GAIN = 0.90
_NORMALIZED_ALIGNMENT_MAX_GAIN = 1.10


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    partial = path.with_name(f"{path.name}.part")
    partial.write_bytes(_canonical_json(payload))
    os.replace(partial, path)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class SongTrackMetadata:
    title: str
    artist: str
    album: str
    duration_frames: int
    sample_rate: int

    def __post_init__(self) -> None:
        if self.duration_frames <= 0 or self.sample_rate <= 0:
            raise ValueError("歌曲时长与采样率必须为正数。")
        if not self.title.strip() and not self.artist.strip():
            raise ValueError("歌曲缓存至少需要标题或艺术家。")
        if any(len(value) > 1_024 for value in (self.title, self.artist, self.album)):
            raise ValueError("歌曲元数据过长。")

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_frames": self.duration_frames,
            "sample_rate": self.sample_rate,
        }

    @property
    def identity(self) -> str:
        payload = {
            "version": 1,
            "title": _normalized_text(self.title),
            "artist": _normalized_text(self.artist),
            "album": _normalized_text(self.album),
            "duration_frames": self.duration_frames,
            "sample_rate": self.sample_rate,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _metadata_matches_airplay_revision(
    cached: SongTrackMetadata,
    requested: SongTrackMetadata,
) -> bool:
    return (
        cached.sample_rate == requested.sample_rate
        and _normalized_text(cached.title) == _normalized_text(requested.title)
        and _normalized_text(cached.artist) == _normalized_text(requested.artist)
        and _normalized_text(cached.album) == _normalized_text(requested.album)
        and abs(cached.duration_frames - requested.duration_frames)
        <= requested.sample_rate // 2
    )


def _normalized_pcm_alignment(
    haystack_pcm: bytes,
    probe_pcm: bytes,
    *,
    channels: int,
    sample_rate: int,
    approximate_offset_frames: int,
) -> int | None:
    """Find a near-identical replay after harmless decoder/sample differences.

    A coarse amplitude-envelope pass narrows the search, then a short raw-wave
    correlation resolves the exact frame. The raw correlation is gain and DC
    invariant, while the explicit gain bound prevents a materially different
    volume variant from reusing stems at the wrong loudness.
    """
    if channels <= 0 or sample_rate <= 0:
        return None
    probe_values = _pcm16_values(probe_pcm)
    haystack_values = _pcm16_values(haystack_pcm)
    if len(probe_values) % channels or len(haystack_values) % channels:
        return None
    probe_frames = len(probe_values) // channels
    haystack_frames = len(haystack_values) // channels
    valid_starts = haystack_frames - probe_frames + 1
    if probe_frames < 8 or valid_starts <= 0:
        return None

    energy_stride = max(1, probe_frames // 4_096)
    channel = max(
        range(channels),
        key=lambda item: sum(
            int(probe_values[frame * channels + item]) ** 2
            for frame in range(0, probe_frames, energy_stride)
        ),
    )
    query = probe_values[channel::channels]
    haystack = haystack_values[channel::channels]

    block_frames = max(1, min(256, sample_rate // 4))
    query_block_count = probe_frames // block_frames
    if query_block_count < 2:
        block_frames = 1
        query_block_count = probe_frames

    def envelope(values: array.array, block_count: int) -> list[float]:
        result: list[float] = []
        for block_index in range(block_count):
            start = block_index * block_frames
            total = 0
            for index in range(start, start + block_frames):
                total += abs(int(values[index]))
            result.append(total / block_frames)
        return result

    query_envelope = envelope(query, query_block_count)
    haystack_block_count = len(haystack) // block_frames
    haystack_envelope = envelope(haystack, haystack_block_count)
    coarse_positions = len(haystack_envelope) - len(query_envelope) + 1
    if coarse_positions <= 0:
        return None

    query_mean = sum(query_envelope) / len(query_envelope)
    query_centered = [value - query_mean for value in query_envelope]
    query_energy = sum(value * value for value in query_centered)
    if query_energy <= 0.0:
        return None

    best_coarse_correlation = -1.0
    best_coarse_offset = 0
    for position in range(coarse_positions):
        candidate = haystack_envelope[position : position + len(query_envelope)]
        candidate_mean = sum(candidate) / len(candidate)
        candidate_energy = 0.0
        dot = 0.0
        for candidate_value, query_value in zip(candidate, query_centered):
            centered = candidate_value - candidate_mean
            candidate_energy += centered * centered
            dot += centered * query_value
        correlation = (
            dot / math.sqrt(candidate_energy * query_energy)
            if candidate_energy > 0.0
            else -1.0
        )
        offset = position * block_frames
        if (
            correlation > best_coarse_correlation
            or correlation == best_coarse_correlation
            and abs(offset - approximate_offset_frames)
            < abs(best_coarse_offset - approximate_offset_frames)
        ):
            best_coarse_correlation = correlation
            best_coarse_offset = offset

    fine_frames = min(probe_frames, max(8, sample_rate // 4))
    fine_starts = list(range(0, probe_frames - fine_frames + 1, fine_frames))
    if fine_starts[-1] != probe_frames - fine_frames:
        fine_starts.append(probe_frames - fine_frames)
    query_start = max(
        fine_starts,
        key=lambda start: sum(
            int(query[index]) ** 2 for index in range(start, start + fine_frames)
        ),
    )
    fine_stride = max(1, min(4, sample_rate // 10_000))
    query_indices = range(query_start, query_start + fine_frames, fine_stride)
    query_raw = [float(query[index]) for index in query_indices]
    raw_mean = sum(query_raw) / len(query_raw)
    raw_centered = [value - raw_mean for value in query_raw]
    raw_energy = sum(value * value for value in raw_centered)
    if raw_energy <= 0.0:
        return None

    fine_radius = max(8, block_frames * 2)
    first_fine = max(0, best_coarse_offset - fine_radius)
    last_fine = min(valid_starts - 1, best_coarse_offset + fine_radius)
    best_correlation = -1.0
    best_gain = 0.0
    best_offset = 0
    for offset in range(first_fine, last_fine + 1):
        candidate_raw = [
            float(haystack[offset + index]) for index in query_indices
        ]
        candidate_mean = sum(candidate_raw) / len(candidate_raw)
        candidate_energy = 0.0
        dot = 0.0
        for candidate_value, query_value in zip(candidate_raw, raw_centered):
            centered = candidate_value - candidate_mean
            candidate_energy += centered * centered
            dot += centered * query_value
        if candidate_energy <= 0.0:
            continue
        correlation = dot / math.sqrt(candidate_energy * raw_energy)
        gain = dot / candidate_energy
        if (
            correlation > best_correlation
            or correlation == best_correlation
            and abs(offset - approximate_offset_frames)
            < abs(best_offset - approximate_offset_frames)
        ):
            best_correlation = correlation
            best_gain = gain
            best_offset = offset

    if (
        best_correlation < _NORMALIZED_ALIGNMENT_MIN_CORRELATION
        or not _NORMALIZED_ALIGNMENT_MIN_GAIN
        <= best_gain
        <= _NORMALIZED_ALIGNMENT_MAX_GAIN
    ):
        return None
    return best_offset


@dataclass(frozen=True)
class SongCacheProfile:
    profile_name: str
    model_filename: str
    stems: tuple[str, ...]
    sample_rate: int
    channels: int
    bits_per_sample: int
    window_seconds: int
    hop_seconds: int
    stable_offset_seconds: int
    overlap_frames: int
    algorithm_version: str = _ALGORITHM_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "stems", tuple(self.stems))
        if not self.profile_name.strip() or not self.model_filename.strip():
            raise ValueError("歌曲缓存必须绑定分离模式和模型。")
        if not self.stems or len(set(self.stems)) != len(self.stems):
            raise ValueError("歌曲缓存音轨定义无效。")
        if any(not _STEM_PATTERN.fullmatch(stem) for stem in self.stems):
            raise ValueError("歌曲缓存音轨名称无效。")
        if self.sample_rate <= 0 or self.channels <= 0 or self.bits_per_sample != 16:
            raise ValueError("歌曲缓存当前要求有效的 PCM16 几何参数。")
        if (
            self.window_seconds <= 0
            or self.hop_seconds <= 0
            or self.stable_offset_seconds < 0
            or self.overlap_frames <= 0
        ):
            raise ValueError("歌曲缓存窗口参数无效。")
        output_end = (
            (self.stable_offset_seconds + self.hop_seconds) * self.sample_rate
            + self.overlap_frames
        )
        if output_end > self.window_seconds * self.sample_rate:
            raise ValueError("歌曲缓存输出区间超出分析窗口。")
        if not self.algorithm_version.strip():
            raise ValueError("歌曲缓存算法版本不能为空。")

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.bits_per_sample // 8

    @property
    def hop_frames(self) -> int:
        return self.sample_rate * self.hop_seconds

    @property
    def output_frames(self) -> int:
        return self.hop_frames + self.overlap_frames

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "model_filename": self.model_filename,
            "stems": self.stems,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
            "window_seconds": self.window_seconds,
            "hop_seconds": self.hop_seconds,
            "stable_offset_seconds": self.stable_offset_seconds,
            "overlap_frames": self.overlap_frames,
            "algorithm_version": self.algorithm_version,
        }


def _song_cache_key(
    audio_identity: str,
    metadata: SongTrackMetadata,
    profile: SongCacheProfile,
) -> str:
    if not _SHA256_PATTERN.fullmatch(audio_identity):
        raise ValueError("歌曲音频标识必须是 SHA-256。")
    return hashlib.sha256(
        _canonical_json(
            {
                "schema_version": _SCHEMA_VERSION,
                "audio_identity": audio_identity,
                "metadata_identity": metadata.identity,
                "profile": profile.to_dict(),
            }
        )
    ).hexdigest()


def _copy_raw_to_wav(
    raw_path: Path,
    destination: Path,
    *,
    frame_count: int,
    profile: SongCacheProfile,
) -> None:
    byte_count = frame_count * profile.bytes_per_frame
    partial = destination.with_name(f"{destination.name}.part")
    with raw_path.open("rb") as source, wave.open(str(partial), "wb") as output:
        output.setnchannels(profile.channels)
        output.setsampwidth(profile.bits_per_sample // 8)
        output.setframerate(profile.sample_rate)
        remaining = byte_count
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError("歌曲缓存原始 PCM 长度不足。")
            output.writeframesraw(block)
            remaining -= len(block)
        output.writeframes(b"")
    os.replace(partial, destination)


def _slice_wav_atomic(
    source_path: Path,
    destination: Path,
    *,
    start_frame: int,
    frame_count: int,
    profile: SongCacheProfile,
) -> None:
    partial = destination.with_name(f"{destination.name}.part")
    with wave.open(str(source_path), "rb") as source:
        if (
            source.getframerate() != profile.sample_rate
            or source.getnchannels() != profile.channels
            or source.getsampwidth() * 8 != profile.bits_per_sample
            or start_frame < 0
            or start_frame + frame_count > source.getnframes()
        ):
            raise RuntimeError("歌曲缓存切片几何参数无效。")
        source.setpos(start_frame)
        frames = source.readframes(frame_count)
        params = source.getparams()
    if len(frames) != frame_count * profile.bytes_per_frame:
        raise RuntimeError("歌曲缓存切片读取不完整。")
    with wave.open(str(partial), "wb") as output:
        output.setparams(params)
        output.writeframes(frames)
    os.replace(partial, destination)


def _concatenate_wav_slices_atomic(
    slices: tuple[tuple[Path, int, int], ...],
    destination: Path,
    *,
    profile: SongCacheProfile,
) -> None:
    if not slices:
        raise ValueError("歌曲缓存组合至少需要一个切片。")
    partial = destination.with_name(f"{destination.name}.part")
    with wave.open(str(partial), "wb") as output:
        output.setnchannels(profile.channels)
        output.setsampwidth(profile.bits_per_sample // 8)
        output.setframerate(profile.sample_rate)
        for source_path, start_frame, frame_count in slices:
            with wave.open(str(source_path), "rb") as source:
                if (
                    source.getframerate() != profile.sample_rate
                    or source.getnchannels() != profile.channels
                    or source.getsampwidth() * 8 != profile.bits_per_sample
                    or start_frame < 0
                    or frame_count <= 0
                    or start_frame + frame_count > source.getnframes()
                ):
                    raise RuntimeError("歌曲缓存组合切片几何参数无效。")
                source.setpos(start_frame)
                frames = source.readframes(frame_count)
            if len(frames) != frame_count * profile.bytes_per_frame:
                raise RuntimeError("歌曲缓存组合切片读取不完整。")
            output.writeframesraw(frames)
        output.writeframes(b"")
    os.replace(partial, destination)


@dataclass(frozen=True)
class SongCacheEntry:
    root: Path
    cache_key: str
    audio_identity: str
    metadata: SongTrackMetadata
    profile: SongCacheProfile
    manifest: dict

    @property
    def frame_count(self) -> int:
        return int(self.manifest["frame_count"])

    @property
    def origin_track_frame(self) -> int:
        return int(self.manifest.get("origin_track_frame", 0))

    @property
    def access_path(self) -> Path:
        return self.root / "access.touch"

    def _audio_path(self, name: str) -> Path:
        item = self.manifest["files"][name]
        filename = str(item["filename"])
        if Path(filename).name != filename:
            raise RuntimeError("歌曲缓存清单包含不安全路径。")
        return self.root / filename

    def align_source(
        self,
        pcm: bytes,
        *,
        approximate_track_start_frame: int,
        search_radius_frames: int | None = None,
    ) -> int | None:
        bytes_per_frame = self.profile.bytes_per_frame
        if not pcm or len(pcm) % bytes_per_frame:
            raise ValueError("待匹配 PCM 必须包含完整音频帧。")
        probe_frames = len(pcm) // bytes_per_frame
        if probe_frames > self.frame_count:
            return None
        radius = (
            self.profile.sample_rate * 8
            if search_radius_frames is None
            else search_radius_frames
        )
        if radius < 0:
            raise ValueError("歌曲缓存搜索半径不得为负数。")
        approximate = approximate_track_start_frame - self.origin_track_frame
        first = max(0, approximate - radius)
        last = min(self.frame_count - probe_frames, approximate + radius)
        if last < first:
            return None

        source_path = self._audio_path("source")
        with wave.open(str(source_path), "rb") as source:
            source.setpos(first)
            haystack = source.readframes(last - first + probe_frames)

        matches: list[int] = []
        offset = haystack.find(pcm)
        while offset >= 0 and len(matches) < 64:
            if offset % bytes_per_frame == 0:
                candidate = first + offset // bytes_per_frame
                if candidate <= last:
                    matches.append(candidate)
            offset = haystack.find(pcm, offset + 1)
        if matches:
            return min(matches, key=lambda value: (abs(value - approximate), value))
        normalized_offset = _normalized_pcm_alignment(
            haystack,
            pcm,
            channels=self.profile.channels,
            sample_rate=self.profile.sample_rate,
            approximate_offset_frames=approximate - first,
        )
        return first + normalized_offset if normalized_offset is not None else None

    def publish_range(
        self,
        *,
        cache_start_frame: int,
        frame_count: int,
        outbox: str | Path,
        sequence: int,
        latency_seconds: float,
    ) -> Path:
        if sequence <= 0 or frame_count <= 0:
            raise ValueError("歌曲缓存发布参数无效。")
        if cache_start_frame < 0 or cache_start_frame + frame_count > self.frame_count:
            raise IndexError("歌曲缓存没有覆盖所请求的播放区间。")
        destination_root = Path(outbox).resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        published: dict[str, str] = {}
        for stem in self.profile.stems:
            filename = f"result-{sequence:08d}-{stem}.wav"
            _slice_wav_atomic(
                self._audio_path(stem),
                destination_root / filename,
                start_frame=cache_start_frame,
                frame_count=frame_count,
                profile=self.profile,
            )
            published[stem] = filename

        manifest_path = destination_root / f"result-{sequence:08d}.json"
        _atomic_json(
            manifest_path,
            {
                "version": 2,
                "sequence": sequence,
                "sample_rate": self.profile.sample_rate,
                "channels": self.profile.channels,
                "window_seconds": self.profile.window_seconds,
                "hop_seconds": self.profile.hop_seconds,
                "stable_offset_seconds": self.profile.stable_offset_seconds,
                "overlap_frames": self.profile.overlap_frames,
                "processing_seconds": 0.0,
                "latency_seconds": round(max(0.0, latency_seconds), 3),
                "cache_hit": True,
                "cache_scope": "song",
                "cache_key": self.cache_key,
                "cached_start_frame": cache_start_frame,
                "track": self.metadata.to_dict(),
                "stems": published,
            },
        )
        self.access_path.touch(exist_ok=True)
        return manifest_path


@dataclass(frozen=True)
class SongCacheSlice:
    entry: SongCacheEntry
    cache_start_frame: int
    frame_count: int

    def __post_init__(self) -> None:
        if self.frame_count <= 0 or self.cache_start_frame < 0:
            raise ValueError("歌曲缓存组合切片参数无效。")
        if self.cache_start_frame + self.frame_count > self.entry.frame_count:
            raise IndexError("歌曲缓存没有覆盖组合切片所请求的播放区间。")


class SongCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.entries = self.root / "entries"
        self.building = self.root / "building"
        self.staging = self.root / "staging"
        for directory in (self.entries, self.building, self.staging):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._validated: set[str] = set()

    def start_build(self, token: str, profile: SongCacheProfile) -> "SongCacheBuilder":
        return SongCacheBuilder(self, token, profile)

    def discard_incomplete_builds(self) -> int:
        """Remove non-published builders left behind by an interrupted worker."""
        with self._lock:
            removed = 0
            for directory in (self.building, self.staging):
                for artifact in directory.iterdir():
                    if artifact.is_dir():
                        shutil.rmtree(artifact, ignore_errors=True)
                    else:
                        artifact.unlink(missing_ok=True)
                    removed += 1
            return removed

    def complete_entry_count(self) -> int:
        """Return the persistent number of structurally complete song entries."""
        with self._lock:
            return sum(
                1
                for manifest_path in self.entries.glob("*/*/manifest.json")
                if (
                    (payload := self._read_manifest(manifest_path)) is not None
                    and payload.get("state") == "complete"
                )
            )

    def publish_composite(
        self,
        *,
        slices: tuple[SongCacheSlice, ...],
        outbox: str | Path,
        sequence: int,
        latency_seconds: float,
    ) -> Path:
        if sequence <= 0 or not slices:
            raise ValueError("歌曲缓存组合发布参数无效。")
        profile = slices[0].entry.profile
        if any(item.entry.profile != profile for item in slices):
            raise ValueError("歌曲缓存组合切片的分离配置不一致。")

        destination_root = Path(outbox).resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for item in slices:
                try:
                    item.entry.root.resolve().relative_to(self.entries)
                except ValueError as error:
                    raise ValueError("歌曲缓存组合切片不属于当前缓存。") from error
                if not self._validate(item.entry):
                    raise RuntimeError("歌曲缓存组合切片已损坏。")

            published: dict[str, str] = {}
            for stem in profile.stems:
                filename = f"result-{sequence:08d}-{stem}.wav"
                _concatenate_wav_slices_atomic(
                    tuple(
                        (
                            item.entry._audio_path(stem),
                            item.cache_start_frame,
                            item.frame_count,
                        )
                        for item in slices
                    ),
                    destination_root / filename,
                    profile=profile,
                )
                published[stem] = filename

            parts = [
                {
                    "cache_key": item.entry.cache_key,
                    "cached_start_frame": item.cache_start_frame,
                    "frame_count": item.frame_count,
                    "track": item.entry.metadata.to_dict(),
                }
                for item in slices
            ]
            composite_key = hashlib.sha256(
                _canonical_json(
                    {
                        "version": 1,
                        "profile": profile.to_dict(),
                        "parts": parts,
                    }
                )
            ).hexdigest()
            manifest_path = destination_root / f"result-{sequence:08d}.json"
            _atomic_json(
                manifest_path,
                {
                    "version": 2,
                    "sequence": sequence,
                    "sample_rate": profile.sample_rate,
                    "channels": profile.channels,
                    "window_seconds": profile.window_seconds,
                    "hop_seconds": profile.hop_seconds,
                    "stable_offset_seconds": profile.stable_offset_seconds,
                    "overlap_frames": profile.overlap_frames,
                    "processing_seconds": 0.0,
                    "latency_seconds": round(max(0.0, latency_seconds), 3),
                    "cache_hit": True,
                    "cache_scope": "song-composite",
                    "cache_key": composite_key,
                    "cache_part_count": len(parts),
                    "cache_parts": parts,
                    "tracks": [item.entry.metadata.to_dict() for item in slices],
                    "stems": published,
                },
            )
            for item in slices:
                item.entry.access_path.touch(exist_ok=True)
            return manifest_path

    @staticmethod
    def _read_manifest(path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _entry_from_payload(path: Path, payload: dict) -> SongCacheEntry:
        profile = SongCacheProfile(**payload["profile"])
        metadata = SongTrackMetadata(**payload["metadata"])
        return SongCacheEntry(
            root=path.parent,
            cache_key=str(payload["cache_key"]),
            audio_identity=str(payload["audio_identity"]),
            metadata=metadata,
            profile=profile,
            manifest=payload,
        )

    def _validate(self, entry: SongCacheEntry) -> bool:
        if entry.cache_key in self._validated:
            return True
        try:
            if (
                entry.manifest.get("schema_version") != _SCHEMA_VERSION
                or entry.manifest.get("state") != "complete"
                or entry.cache_key
                != _song_cache_key(entry.audio_identity, entry.metadata, entry.profile)
                or entry.frame_count != entry.metadata.duration_frames
            ):
                return False
            files = entry.manifest["files"]
            if set(files) != {"source", *entry.profile.stems}:
                return False
            for name in ("source", *entry.profile.stems):
                item = files[name]
                path = entry._audio_path(name)
                if not path.is_file() or path.stat().st_size != int(item["bytes"]):
                    return False
                if _file_sha256(path) != item["sha256"]:
                    return False
                with wave.open(str(path), "rb") as audio:
                    if (
                        audio.getframerate() != entry.profile.sample_rate
                        or audio.getnchannels() != entry.profile.channels
                        or audio.getsampwidth() * 8 != entry.profile.bits_per_sample
                        or audio.getnframes() != entry.frame_count
                    ):
                        return False
        except (KeyError, TypeError, ValueError, OSError, RuntimeError, wave.Error):
            return False
        self._validated.add(entry.cache_key)
        return True

    def lookup(
        self,
        metadata: SongTrackMetadata,
        profile: SongCacheProfile,
    ) -> list[SongCacheEntry]:
        with self._lock:
            candidates: list[tuple[int, SongCacheEntry]] = []
            for manifest_path in self.entries.glob("*/*/manifest.json"):
                payload = self._read_manifest(manifest_path)
                try:
                    if (
                        payload is None
                        or payload.get("state") != "complete"
                        or payload.get("profile")
                        != json.loads(_canonical_json(profile.to_dict()).decode("utf-8"))
                    ):
                        continue
                    entry = self._entry_from_payload(manifest_path, payload)
                    if not _metadata_matches_airplay_revision(entry.metadata, metadata):
                        continue
                except (KeyError, TypeError, ValueError):
                    shutil.rmtree(manifest_path.parent, ignore_errors=True)
                    continue
                if not self._validate(entry):
                    self._validated.discard(entry.cache_key)
                    shutil.rmtree(entry.root, ignore_errors=True)
                    continue
                entry.access_path.touch(exist_ok=True)
                candidates.append((entry.access_path.stat().st_mtime_ns, entry))
            return [entry for _, entry in sorted(candidates, key=lambda item: item[0], reverse=True)]

    def _publish_builder(self, builder: "SongCacheBuilder") -> SongCacheEntry | None:
        with self._lock:
            metadata = builder.metadata
            if (
                metadata is None
                or builder.first_track_start_frame is None
                or builder.first_track_start_frame > builder.profile.sample_rate * 2
                or builder.frame_count < metadata.duration_frames
                or metadata.sample_rate != builder.profile.sample_rate
            ):
                builder.discard()
                return None

            frame_count = metadata.duration_frames
            stage = self.staging / uuid.uuid4().hex
            stage.mkdir(parents=True)
            source_wav = stage / "source.wav"
            _copy_raw_to_wav(
                builder.source_raw,
                source_wav,
                frame_count=frame_count,
                profile=builder.profile,
            )
            audio_identity = pcm_content_sha256(source_wav)
            cache_key = _song_cache_key(audio_identity, metadata, builder.profile)
            files: dict[str, dict[str, object]] = {}

            def register(name: str, path: Path) -> None:
                files[name] = {
                    "filename": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }

            register("source", source_wav)
            for stem in builder.profile.stems:
                destination = stage / f"{stem}.wav"
                _copy_raw_to_wav(
                    builder.stem_raws[stem],
                    destination,
                    frame_count=frame_count,
                    profile=builder.profile,
                )
                register(stem, destination)

            payload = {
                "schema_version": _SCHEMA_VERSION,
                "state": "complete",
                "cache_key": cache_key,
                "audio_identity": audio_identity,
                "metadata_identity": metadata.identity,
                "metadata": metadata.to_dict(),
                "profile": builder.profile.to_dict(),
                "origin_track_frame": 0,
                "frame_count": frame_count,
                "files": files,
                "completed_at_ns": time.time_ns(),
            }
            _atomic_json(stage / "manifest.json", payload)
            (stage / "access.touch").touch()
            destination_root = self.entries / cache_key[:2] / cache_key
            destination_root.parent.mkdir(parents=True, exist_ok=True)
            if destination_root.exists():
                shutil.rmtree(stage, ignore_errors=True)
            else:
                os.replace(stage, destination_root)
            builder.discard()

            manifest_path = destination_root / "manifest.json"
            existing = self._read_manifest(manifest_path)
            if existing is None:
                return None
            entry = self._entry_from_payload(manifest_path, existing)
            if not self._validate(entry):
                shutil.rmtree(destination_root, ignore_errors=True)
                return None
            return entry

    def prune_to_quota(self, maximum_bytes: int) -> list[str]:
        if maximum_bytes < 0:
            raise ValueError("歌曲缓存容量上限不得为负数。")
        with self._lock:
            entries: list[tuple[int, str, Path, int]] = []
            total = 0
            for manifest_path in self.entries.glob("*/*/manifest.json"):
                payload = self._read_manifest(manifest_path)
                if payload is None or payload.get("state") != "complete":
                    continue
                root = manifest_path.parent
                size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
                access = root / "access.touch"
                used = access.stat().st_mtime_ns if access.exists() else manifest_path.stat().st_mtime_ns
                key = str(payload.get("cache_key", root.name))
                entries.append((used, key, root, size))
                total += size
            removed: list[str] = []
            for _, key, root, size in sorted(entries):
                if total <= maximum_bytes:
                    break
                shutil.rmtree(root, ignore_errors=True)
                self._validated.discard(key)
                total -= size
                removed.append(key)
            return removed


class SongCacheBuilder:
    def __init__(self, cache: SongCache, token: str, profile: SongCacheProfile) -> None:
        if not token.strip():
            raise ValueError("歌曲缓存构建标识不能为空。")
        self.cache = cache
        self.profile = profile
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.root = cache.building / token_hash
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.source_raw = self.root / "source.pcm"
        self.stem_raws = {stem: self.root / f"{stem}.pcm" for stem in profile.stems}
        self.source_raw.touch()
        for path in self.stem_raws.values():
            path.touch()
        self.metadata: SongTrackMetadata | None = None
        self.first_track_start_frame: int | None = None
        self.last_stream_end_frame: int | None = None
        self.frame_count = 0
        self._finished = False

    def append(
        self,
        *,
        stream_start_frame: int,
        track_start_frame: int,
        metadata: SongTrackMetadata,
        source_pcm: bytes,
        stems: Mapping[str, bytes],
    ) -> None:
        if self._finished:
            raise RuntimeError("歌曲缓存构建已结束。")
        if stream_start_frame < 0 or track_start_frame < 0:
            raise ValueError("歌曲缓存时间轴不得为负数。")
        if metadata.sample_rate != self.profile.sample_rate:
            raise ValueError("歌曲元数据采样率与缓存配置不一致。")
        if set(stems) != set(self.profile.stems):
            raise ValueError("歌曲缓存片段必须包含全部音轨。")
        bytes_per_frame = self.profile.bytes_per_frame
        if not source_pcm or len(source_pcm) % bytes_per_frame:
            raise ValueError("歌曲缓存原声片段不是完整 PCM 帧。")
        frames = len(source_pcm) // bytes_per_frame
        if any(len(stems[stem]) != len(source_pcm) for stem in self.profile.stems):
            raise ValueError("歌曲缓存各轨片段长度不一致。")
        if self.last_stream_end_frame is not None and stream_start_frame != self.last_stream_end_frame:
            raise RuntimeError("歌曲缓存时间轴不连续。")
        if self.metadata is not None and metadata.duration_frames != self.metadata.duration_frames:
            raise RuntimeError("同一歌曲修订的时长发生变化。")

        self.metadata = metadata
        if self.first_track_start_frame is None:
            self.first_track_start_frame = track_start_frame
        with self.source_raw.open("ab") as output:
            output.write(source_pcm)
        for stem in self.profile.stems:
            with self.stem_raws[stem].open("ab") as output:
                output.write(stems[stem])
        self.frame_count += frames
        self.last_stream_end_frame = stream_start_frame + frames

    def finalize(self) -> SongCacheEntry | None:
        if self._finished:
            raise RuntimeError("歌曲缓存构建已结束。")
        self._finished = True
        return self.cache._publish_builder(self)

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _pcm16_values(pcm: bytes) -> array.array:
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _pcm16_bytes(values: array.array) -> bytes:
    result = array.array("h", values)
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def _round_pcm16(value: float) -> int:
    rounded = math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)
    return max(-32_768, min(32_767, rounded))


class Pcm16OverlapStitcher:
    def __init__(
        self,
        *,
        stems: tuple[str, ...],
        channels: int,
        hop_frames: int,
        overlap_frames: int,
    ) -> None:
        if (
            not stems
            or len(set(stems)) != len(stems)
            or channels <= 0
            or hop_frames <= 0
            or overlap_frames <= 0
            or overlap_frames > hop_frames
        ):
            raise ValueError("PCM 拼接器参数无效。")
        self.stems = stems
        self.channels = channels
        self.hop_frames = hop_frames
        self.overlap_frames = overlap_frames
        self._tails: dict[str, array.array] = {}

    def reset(self) -> None:
        self._tails.clear()

    def push(self, chunks: Mapping[str, bytes]) -> dict[str, bytes]:
        if set(chunks) != set(self.stems):
            raise ValueError("PCM 拼接块缺少音轨。")
        expected_samples = (self.hop_frames + self.overlap_frames) * self.channels
        output: dict[str, bytes] = {}
        for stem in self.stems:
            current = _pcm16_values(chunks[stem])
            if len(current) != expected_samples:
                raise ValueError("PCM 拼接块长度无效。")
            hop_samples = self.hop_frames * self.channels
            overlap_samples = self.overlap_frames * self.channels
            stitched = array.array("h", current[:hop_samples])
            previous = self._tails.get(stem)
            if previous is not None:
                for frame in range(self.overlap_frames):
                    next_weight = (
                        0.5
                        if self.overlap_frames == 1
                        else frame / (self.overlap_frames - 1)
                    )
                    previous_weight = 1.0 - next_weight
                    for channel in range(self.channels):
                        index = frame * self.channels + channel
                        stitched[index] = _round_pcm16(
                            previous[index] * previous_weight
                            + current[index] * next_weight
                        )
            self._tails[stem] = array.array(
                "h",
                current[hop_samples : hop_samples + overlap_samples],
            )
            output[stem] = _pcm16_bytes(stitched)
        return output
