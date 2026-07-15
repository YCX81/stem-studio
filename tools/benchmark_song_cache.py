from __future__ import annotations

import argparse
import array
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stemstudio.song_cache import SongCache, SongCacheProfile, SongTrackMetadata


def _pcm_hop(frame_count: int) -> bytes:
    samples = array.array("h")
    for frame in range(frame_count):
        value = (((frame * 73) ^ ((frame >> 8) * 151)) & 0xFFFF) - 32768
        samples.extend((value, -value if value != -32768 else 32767))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def run_benchmark(*, duration_seconds: int, repeats: int) -> dict[str, object]:
    if duration_seconds < 12 or duration_seconds % 6:
        raise ValueError("duration_seconds must be at least 12 and divisible by 6")
    if repeats < 1:
        raise ValueError("repeats must be positive")

    sample_rate = 44_100
    hop_frames = sample_rate * 6
    overlap_frames = sample_rate // 10
    duration_frames = sample_rate * duration_seconds
    stems = ("vocals", "drums", "bass", "guitar", "piano", "other")
    profile = SongCacheProfile(
        profile_name="六轨 · 加吉他/钢琴",
        model_filename="htdemucs_6s.yaml",
        stems=stems,
        sample_rate=sample_rate,
        channels=2,
        bits_per_sample=16,
        window_seconds=12,
        hop_seconds=6,
        stable_offset_seconds=0,
        overlap_frames=overlap_frames,
    )
    metadata = SongTrackMetadata(
        title="Synthetic Product Acceptance",
        artist="Stem Studio",
        album="Cache Benchmark",
        duration_frames=duration_frames,
        sample_rate=sample_rate,
    )
    hop_pcm = _pcm_hop(hop_frames)
    probe_pcm = hop_pcm + hop_pcm[: overlap_frames * profile.bytes_per_frame]
    approximate_start = sample_rate * 60 if duration_seconds >= 72 else 0

    build_root = PROJECT_ROOT / "build"
    build_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="song-cache-benchmark-", dir=build_root) as temp:
        root = Path(temp)
        cache = SongCache(root / "song-cache")
        builder = cache.start_build("real-geometry", profile)
        build_started = time.perf_counter()
        for start in range(0, duration_frames, hop_frames):
            builder.append(
                stream_start_frame=start,
                track_start_frame=start,
                metadata=metadata,
                source_pcm=hop_pcm,
                stems={stem: hop_pcm for stem in stems},
            )
        entry = builder.finalize()
        build_seconds = time.perf_counter() - build_started
        if entry is None:
            raise RuntimeError("synthetic song cache did not finalize")

        cache_bytes = sum(
            path.stat().st_size for path in entry.root.iterdir() if path.is_file()
        )
        cold_cache = SongCache(root / "song-cache")
        cold_started = time.perf_counter()
        candidates = cold_cache.lookup(metadata, profile)
        cold_lookup_seconds = time.perf_counter() - cold_started
        if len(candidates) != 1:
            raise RuntimeError("cold cache lookup did not return exactly one song")
        aligned = candidates[0].align_source(
            probe_pcm,
            approximate_track_start_frame=approximate_start,
        )
        if aligned is None:
            raise RuntimeError("cold cache PCM alignment failed")
        candidates[0].publish_range(
            cache_start_frame=aligned,
            frame_count=profile.output_frames,
            outbox=root / "outbox",
            sequence=1,
            latency_seconds=0.0,
        )
        cold_total_seconds = time.perf_counter() - cold_started

        warm_seconds: list[float] = []
        for _ in range(repeats):
            started = time.perf_counter()
            warm_candidates = cold_cache.lookup(metadata, profile)
            warm_aligned = warm_candidates[0].align_source(
                probe_pcm,
                approximate_track_start_frame=approximate_start,
            )
            if warm_aligned is None:
                raise RuntimeError("warm cache PCM alignment failed")
            warm_candidates[0].publish_range(
                cache_start_frame=warm_aligned,
                frame_count=profile.output_frames,
                outbox=root / "outbox",
                sequence=1,
                latency_seconds=0.0,
            )
            warm_seconds.append(time.perf_counter() - started)

        result = {
            "duration_seconds": duration_seconds,
            "tracks": len(stems),
            "cache_bytes": cache_bytes,
            "build_seconds": round(build_seconds, 3),
            "cold_lookup_seconds": round(cold_lookup_seconds, 3),
            "cold_total_seconds": round(cold_total_seconds, 3),
            "warm_min_seconds": round(min(warm_seconds), 3),
            "warm_median_seconds": round(statistics.median(warm_seconds), 3),
            "warm_max_seconds": round(max(warm_seconds), 3),
            "realtime_deadline_seconds": 6.0,
        }
        if cold_total_seconds >= 6.0 or max(warm_seconds) >= 6.0:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark realistic six-track persistent song-cache replay."
    )
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    print(
        json.dumps(
            run_benchmark(
                duration_seconds=args.duration_seconds,
                repeats=args.repeats,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
