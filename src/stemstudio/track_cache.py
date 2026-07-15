from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Mapping


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STEM_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CACHE_SCHEMA_VERSION = 1
_DEFAULT_ALGORITHM_VERSION = "live-hop-crossfade-v1"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    partial.write_bytes(_canonical_json(payload))
    os.replace(partial, path)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    shutil.copyfile(source, partial)
    os.replace(partial, destination)


def pcm_content_sha256(path: str | Path) -> str:
    """Hash canonical WAV geometry and decoded PCM bytes, never the file path."""
    source_path = Path(path).resolve()
    digest = hashlib.sha256()
    with wave.open(str(source_path), "rb") as audio:
        geometry = {
            "channels": audio.getnchannels(),
            "sample_width": audio.getsampwidth(),
            "sample_rate": audio.getframerate(),
            "frames": audio.getnframes(),
            "compression": audio.getcomptype(),
        }
        digest.update(b"stem-studio-pcm-identity-v1\0")
        digest.update(_canonical_json(geometry))
        while frames := audio.readframes(64 * 1024):
            digest.update(frames)
    return digest.hexdigest()


@dataclass(frozen=True)
class TrackCacheSpec:
    audio_identity: str
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
    algorithm_version: str = _DEFAULT_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.audio_identity):
            raise ValueError("歌曲音频标识必须是 SHA-256。")
        if not self.profile_name.strip() or not self.model_filename.strip():
            raise ValueError("缓存必须绑定分离模式和模型。")
        if not self.stems or len(set(self.stems)) != len(self.stems):
            raise ValueError("缓存音轨定义无效。")
        if any(not _STEM_PATTERN.fullmatch(stem) for stem in self.stems):
            raise ValueError("缓存音轨名称无效。")
        if self.sample_rate <= 0 or self.channels <= 0 or self.bits_per_sample <= 0:
            raise ValueError("缓存音频参数必须为正数。")
        if (
            self.window_seconds <= 0
            or self.hop_seconds <= 0
            or self.stable_offset_seconds < 0
            or self.overlap_frames < 0
        ):
            raise ValueError("缓存窗口参数无效。")
        output_end = (
            (self.stable_offset_seconds + self.hop_seconds) * self.sample_rate
            + self.overlap_frames
        )
        if output_end > self.window_seconds * self.sample_rate:
            raise ValueError("缓存稳定输出区间超出分析窗口。")
        if not self.algorithm_version.strip():
            raise ValueError("缓存算法版本不能为空。")

    def to_dict(self) -> dict:
        return {
            "audio_identity": self.audio_identity,
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

    @property
    def cache_key(self) -> str:
        payload = {"schema_version": _CACHE_SCHEMA_VERSION, "spec": self.to_dict()}
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class TrackCacheEntry:
    root: Path
    spec: TrackCacheSpec
    manifest: dict

    @property
    def access_path(self) -> Path:
        return self.root / "access.touch"

    @property
    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def _chunk(self, chunk_index: int) -> dict:
        for chunk in self.manifest.get("chunks", []):
            if int(chunk.get("index", -1)) == chunk_index:
                return chunk
        raise IndexError(f"缓存中不存在第 {chunk_index} 个音频块。")

    def chunk_path(self, chunk_index: int, stem: str) -> Path:
        if stem not in self.spec.stems:
            raise KeyError(stem)
        item = self._chunk(chunk_index)["stems"][stem]
        filename = str(item["filename"])
        if Path(filename).name != filename:
            raise RuntimeError("缓存清单包含不安全的文件路径。")
        return self.root / filename

    def publish_chunk(self, chunk_index: int, outbox: str | Path, sequence: int) -> Path:
        if sequence <= 0:
            raise ValueError("实时结果序号必须为正数。")
        destination_root = Path(outbox).resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        published: dict[str, str] = {}
        for stem in self.spec.stems:
            filename = f"result-{sequence:08d}-{stem}.wav"
            _atomic_copy(self.chunk_path(chunk_index, stem), destination_root / filename)
            published[stem] = filename
        manifest_path = destination_root / f"result-{sequence:08d}.json"
        _atomic_json(
            manifest_path,
            {
                "version": 2,
                "sequence": sequence,
                "sample_rate": self.spec.sample_rate,
                "channels": self.spec.channels,
                "window_seconds": self.spec.window_seconds,
                "hop_seconds": self.spec.hop_seconds,
                "stable_offset_seconds": self.spec.stable_offset_seconds,
                "overlap_frames": self.spec.overlap_frames,
                "processing_seconds": 0.0,
                "latency_seconds": 0.0,
                "cache_hit": True,
                "cache_scope": "window",
                "cache_key": self.spec.cache_key,
                "cache_chunk_index": chunk_index,
                "stems": published,
            },
        )
        return manifest_path


class TrackCache:
    """Content-addressed, integrity-checked cache for lossless live stem chunks."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _entry_root(self, spec: TrackCacheSpec) -> Path:
        key = spec.cache_key
        return self.root / key[:2] / key

    def _manifest_path(self, spec: TrackCacheSpec) -> Path:
        return self._entry_root(spec) / "manifest.json"

    @staticmethod
    def _read_manifest(path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _spec_matches(payload: dict, spec: TrackCacheSpec) -> bool:
        manifest_spec = payload.get("spec")
        expected = json.loads(_canonical_json(spec.to_dict()).decode("utf-8"))
        return manifest_spec == expected

    def store_chunk(
        self,
        spec: TrackCacheSpec,
        chunk_index: int,
        stems: Mapping[str, str | Path],
    ) -> None:
        if chunk_index < 0:
            raise ValueError("缓存块序号不得为负数。")
        if set(stems) != set(spec.stems):
            raise ValueError("缓存块必须包含当前模式的全部音轨。")
        with self._lock:
            entry_root = self._entry_root(spec)
            manifest_path = entry_root / "manifest.json"
            payload = self._read_manifest(manifest_path)
            if payload is None:
                payload = {
                    "schema_version": _CACHE_SCHEMA_VERSION,
                    "state": "building",
                    "cache_key": spec.cache_key,
                    "spec": spec.to_dict(),
                    "created_at_ns": time.time_ns(),
                    "chunks": [],
                    "metadata": {},
                }
            elif not self._spec_matches(payload, spec):
                raise RuntimeError("缓存键与已有清单参数不一致。")
            elif payload.get("state") == "complete":
                raise RuntimeError("完整歌曲缓存不可被实时块覆盖。")

            stem_items: dict[str, dict] = {}
            for stem in spec.stems:
                source = Path(stems[stem]).resolve()
                if not source.is_file():
                    raise FileNotFoundError(source)
                filename = f"chunk-{chunk_index:06d}-{stem}.wav"
                destination = entry_root / filename
                _atomic_copy(source, destination)
                stem_items[stem] = {
                    "filename": filename,
                    "sha256": _file_sha256(destination),
                    "bytes": destination.stat().st_size,
                }

            chunks = [
                chunk for chunk in payload.get("chunks", [])
                if int(chunk.get("index", -1)) != chunk_index
            ]
            chunks.append({"index": chunk_index, "stems": stem_items})
            payload["chunks"] = sorted(chunks, key=lambda chunk: int(chunk["index"]))
            payload["updated_at_ns"] = time.time_ns()
            _atomic_json(manifest_path, payload)

    def finalize(
        self,
        spec: TrackCacheSpec,
        chunk_count: int,
        metadata: Mapping[str, object] | None = None,
    ) -> TrackCacheEntry:
        if chunk_count <= 0:
            raise ValueError("完整歌曲缓存至少需要一个音频块。")
        with self._lock:
            manifest_path = self._manifest_path(spec)
            payload = self._read_manifest(manifest_path)
            if payload is None or not self._spec_matches(payload, spec):
                raise RuntimeError("没有可完成的歌曲缓存。")
            indices = [int(chunk.get("index", -1)) for chunk in payload.get("chunks", [])]
            if indices != list(range(chunk_count)):
                raise RuntimeError("歌曲缓存块不连续，不能标记为完整。")
            payload["state"] = "complete"
            payload["chunk_count"] = chunk_count
            payload["metadata"] = dict(metadata or {})
            payload["completed_at_ns"] = time.time_ns()
            _atomic_json(manifest_path, payload)
            access_path = manifest_path.parent / "access.touch"
            access_path.touch(exist_ok=True)
            entry = TrackCacheEntry(manifest_path.parent, spec, payload)
            if not self._validate(entry):
                raise RuntimeError("歌曲缓存完整性校验失败。")
            return entry

    def lookup(self, spec: TrackCacheSpec) -> TrackCacheEntry | None:
        with self._lock:
            manifest_path = self._manifest_path(spec)
            payload = self._read_manifest(manifest_path)
            if payload is None:
                if manifest_path.exists():
                    shutil.rmtree(manifest_path.parent, ignore_errors=True)
                return None
            if payload.get("state") != "complete":
                return None
            if (
                payload.get("schema_version") != _CACHE_SCHEMA_VERSION
                or payload.get("cache_key") != spec.cache_key
                or not self._spec_matches(payload, spec)
            ):
                shutil.rmtree(manifest_path.parent, ignore_errors=True)
                return None
            entry = TrackCacheEntry(manifest_path.parent, spec, payload)
            if not self._validate(entry):
                shutil.rmtree(manifest_path.parent, ignore_errors=True)
                return None
            entry.access_path.touch(exist_ok=True)
            return entry

    @staticmethod
    def _validate(entry: TrackCacheEntry) -> bool:
        try:
            chunks = entry.manifest["chunks"]
            if len(chunks) != int(entry.manifest["chunk_count"]):
                return False
            if [int(chunk["index"]) for chunk in chunks] != list(range(len(chunks))):
                return False
            for chunk_index, chunk in enumerate(chunks):
                if set(chunk["stems"]) != set(entry.spec.stems):
                    return False
                for stem in entry.spec.stems:
                    item = chunk["stems"][stem]
                    path = entry.chunk_path(chunk_index, stem)
                    if not path.is_file() or path.stat().st_size != int(item["bytes"]):
                        return False
                    if _file_sha256(path) != item["sha256"]:
                        return False
        except (KeyError, TypeError, ValueError, OSError, RuntimeError, IndexError):
            return False
        return True

    def prune_to_quota(self, maximum_bytes: int) -> list[str]:
        if maximum_bytes < 0:
            raise ValueError("缓存容量上限不得为负数。")
        with self._lock:
            candidates: list[tuple[int, str, Path, int]] = []
            total = 0
            for manifest_path in self.root.glob("*/*/manifest.json"):
                payload = self._read_manifest(manifest_path)
                if payload is None or payload.get("state") != "complete":
                    continue
                entry_root = manifest_path.parent
                size = sum(path.stat().st_size for path in entry_root.rglob("*") if path.is_file())
                access_path = entry_root / "access.touch"
                try:
                    last_used = access_path.stat().st_mtime_ns
                except OSError:
                    last_used = manifest_path.stat().st_mtime_ns
                key = str(payload.get("cache_key", entry_root.name))
                candidates.append((last_used, key, entry_root, size))
                total += size

            removed: list[str] = []
            for _, key, entry_root, size in sorted(candidates):
                if total <= maximum_bytes:
                    break
                shutil.rmtree(entry_root)
                total -= size
                removed.append(key)
            return removed
