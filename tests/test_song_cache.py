import json
import os
import struct
import wave
from pathlib import Path

from stemstudio.song_cache import (
    Pcm16OverlapStitcher,
    SongCache,
    SongCacheProfile,
    SongCacheSlice,
    SongTrackMetadata,
)


def _pcm(frames: range, *, scale: int = 1) -> bytes:
    samples = []
    for frame in frames:
        value = max(-32_768, min(32_767, frame * scale - 200))
        samples.extend((value, -value))
    return struct.pack(f"<{len(samples)}h", *samples)


def _profile() -> SongCacheProfile:
    return SongCacheProfile(
        profile_name="人声 / 伴奏 · 高质量",
        model_filename="quality.ckpt",
        stems=("vocals", "instrumental"),
        sample_rate=10,
        channels=2,
        bits_per_sample=16,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
        overlap_frames=1,
    )


def _metadata(title: str = "Same Song") -> SongTrackMetadata:
    return SongTrackMetadata(
        title=title,
        artist="Artist",
        album="Album",
        duration_frames=60,
        sample_rate=10,
    )


def _build_song(
    cache: SongCache,
    token: str,
    *,
    source_scale: int = 1,
    title: str = "Same Song",
):
    builder = cache.start_build(token, _profile())
    metadata = _metadata(title)
    for index, start in enumerate((0, 20, 40)):
        frames = range(start, start + 20)
        builder.append(
            stream_start_frame=100 + start,
            track_start_frame=start,
            metadata=metadata,
            source_pcm=_pcm(frames, scale=source_scale),
            stems={
                "vocals": _pcm(frames, scale=2),
                "instrumental": _pcm(frames, scale=3),
            },
        )
        assert builder.frame_count == (index + 1) * 20
    return builder.finalize()


def test_song_cache_replays_arbitrary_aligned_range_without_model_output(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    entry = _build_song(cache, "session-1")

    assert entry is not None
    cache = SongCache(tmp_path / "songs")
    candidates = cache.lookup(_metadata(), _profile())
    assert [candidate.cache_key for candidate in candidates] == [entry.cache_key]

    current_source = _pcm(range(13, 34))
    aligned = candidates[0].align_source(
        current_source,
        approximate_track_start_frame=12,
        search_radius_frames=5,
    )
    assert aligned == 13

    manifest_path = candidates[0].publish_range(
        cache_start_frame=aligned,
        frame_count=21,
        outbox=tmp_path / "outbox",
        sequence=42,
        latency_seconds=8.25,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cache_hit"] is True
    assert manifest["cache_scope"] == "song"
    assert manifest["processing_seconds"] == 0.0
    assert manifest["cached_start_frame"] == 13
    assert set(manifest["stems"]) == {"vocals", "instrumental"}
    with wave.open(str(tmp_path / "outbox" / manifest["stems"]["vocals"]), "rb") as audio:
        assert audio.getnframes() == 21
        assert audio.readframes(21) == _pcm(range(13, 34), scale=2)


def test_song_cache_alignment_tolerates_small_replay_pcm_differences(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    builder = cache.start_build("normalized-replay", _profile())

    def irregular_pcm(frames: range) -> bytes:
        samples: list[int] = []
        for frame in frames:
            left = ((frame * frame * 97 + frame * 31) % 1_600) - 800
            right = ((frame * frame * 53 + frame * 71) % 1_400) - 700
            samples.extend((left, right))
        return struct.pack(f"<{len(samples)}h", *samples)

    for start in (0, 20, 40):
        frames = range(start, start + 20)
        builder.append(
            stream_start_frame=100 + start,
            track_start_frame=start,
            metadata=_metadata(),
            source_pcm=irregular_pcm(frames),
            stems={
                "vocals": _pcm(frames, scale=2),
                "instrumental": _pcm(frames, scale=3),
            },
        )
    entry = builder.finalize()
    assert entry is not None
    exact = irregular_pcm(range(13, 34))
    samples = list(struct.unpack(f"<{len(exact) // 2}h", exact))
    for index in range(0, len(samples), 7):
        samples[index] += 2 if index % 2 == 0 else -2
    replay = struct.pack(f"<{len(samples)}h", *samples)

    aligned = entry.align_source(
        replay,
        approximate_track_start_frame=12,
        search_radius_frames=5,
    )

    assert replay != exact
    assert aligned == 13


def test_song_cache_lookup_tolerates_subsecond_airplay_duration_drift(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    entry = _build_song(cache, "duration-drift")
    assert entry is not None
    replay_metadata = SongTrackMetadata(
        title="Same Song",
        artist="Artist",
        album="Album",
        duration_frames=59,
        sample_rate=10,
    )
    different_duration = SongTrackMetadata(
        title="Same Song",
        artist="Artist",
        album="Album",
        duration_frames=54,
        sample_rate=10,
    )

    assert [item.cache_key for item in cache.lookup(replay_metadata, _profile())] == [
        entry.cache_key
    ]
    assert cache.lookup(different_duration, _profile()) == []


def test_song_cache_default_alignment_covers_large_airplay_anchor_correction(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    entry = _build_song(cache, "anchor-correction")
    assert entry is not None

    aligned = entry.align_source(
        _pcm(range(30, 41)),
        approximate_track_start_frame=0,
    )

    assert aligned == 30


def test_complete_entry_count_persists_and_deduplicates_same_song(tmp_path: Path) -> None:
    root = tmp_path / "songs"
    cache = SongCache(root)

    assert cache.complete_entry_count() == 0
    first = _build_song(cache, "session-1")
    assert first is not None
    assert cache.complete_entry_count() == 1

    reopened = SongCache(root)
    duplicate = _build_song(reopened, "session-2")
    assert duplicate is not None
    assert duplicate.cache_key == first.cache_key
    assert reopened.complete_entry_count() == 1


def test_discard_incomplete_builds_preserves_complete_content_hash_entries(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    complete = _build_song(cache, "complete-session")
    assert complete is not None
    builder = cache.start_build("interrupted-session", _profile())
    builder.append(
        stream_start_frame=500,
        track_start_frame=0,
        metadata=_metadata("Interrupted Song"),
        source_pcm=_pcm(range(0, 20)),
        stems={
            "vocals": _pcm(range(0, 20), scale=2),
            "instrumental": _pcm(range(0, 20), scale=3),
        },
    )
    interrupted_stage = cache.staging / "interrupted-publish"
    interrupted_stage.mkdir()
    (interrupted_stage / "source.wav.part").write_bytes(b"partial")

    removed = cache.discard_incomplete_builds()

    assert removed == 2
    assert list(cache.building.iterdir()) == []
    assert list(cache.staging.iterdir()) == []
    assert cache.complete_entry_count() == 1
    assert (complete.root / "manifest.json").is_file()


def test_same_metadata_can_keep_distinct_audio_variants(tmp_path: Path) -> None:
    cache = SongCache(tmp_path / "songs")
    first = _build_song(cache, "session-1", source_scale=1)
    second = _build_song(cache, "session-2", source_scale=2)

    assert first is not None and second is not None
    assert first.cache_key != second.cache_key
    candidates = cache.lookup(_metadata(), _profile())
    assert {entry.cache_key for entry in candidates} == {
        first.cache_key,
        second.cache_key,
    }
    probe = _pcm(range(20, 41), scale=2)
    matches = [
        entry
        for entry in candidates
        if entry.align_source(
            probe,
            approximate_track_start_frame=20,
            search_radius_frames=1,
        )
        is not None
    ]
    assert [entry.cache_key for entry in matches] == [second.cache_key]


def test_incomplete_song_is_not_published_as_reusable_cache(tmp_path: Path) -> None:
    cache = SongCache(tmp_path / "songs")
    builder = cache.start_build("partial", _profile())
    builder.append(
        stream_start_frame=0,
        track_start_frame=20,
        metadata=_metadata(),
        source_pcm=_pcm(range(20, 40)),
        stems={
            "vocals": _pcm(range(20, 40), scale=2),
            "instrumental": _pcm(range(20, 40), scale=3),
        },
    )

    assert builder.finalize() is None
    assert cache.lookup(_metadata(), _profile()) == []


def test_pcm_overlap_stitcher_matches_continuous_timeline() -> None:
    stitcher = Pcm16OverlapStitcher(
        stems=("vocals",),
        channels=2,
        hop_frames=4,
        overlap_frames=2,
    )
    first = _pcm(range(0, 6))
    second = _pcm(range(4, 10))

    assert stitcher.push({"vocals": first})["vocals"] == _pcm(range(0, 4))
    assert stitcher.push({"vocals": second})["vocals"] == _pcm(range(4, 8))

    stitcher.reset()
    assert stitcher.push({"vocals": second})["vocals"] == _pcm(range(4, 8))


def test_corrupt_song_cache_is_removed_and_becomes_safe_miss(tmp_path: Path) -> None:
    cache = SongCache(tmp_path / "songs")
    entry = _build_song(cache, "session-1")
    assert entry is not None
    (entry.root / "vocals.wav").write_bytes(b"corrupt")

    reopened = SongCache(tmp_path / "songs")

    assert reopened.lookup(_metadata(), _profile()) == []
    assert not entry.root.exists()


def test_song_cache_quota_prunes_least_recently_used_entry(tmp_path: Path) -> None:
    cache = SongCache(tmp_path / "songs")
    first = _build_song(cache, "session-1", source_scale=1)
    second = _build_song(cache, "session-2", source_scale=2)
    assert first is not None and second is not None
    os.utime(first.access_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second.access_path, ns=(2_000_000_000, 2_000_000_000))
    second_size = sum(path.stat().st_size for path in second.root.rglob("*") if path.is_file())

    removed = cache.prune_to_quota(second_size + 1)

    assert removed == [first.cache_key]
    assert not first.root.exists()
    assert second.root.exists()


def test_song_cache_composes_one_output_chunk_across_two_cached_tracks(
    tmp_path: Path,
) -> None:
    cache = SongCache(tmp_path / "songs")
    first = _build_song(cache, "first", title="First Song")
    second = _build_song(
        cache,
        "second",
        source_scale=2,
        title="Second Song",
    )
    assert first is not None and second is not None

    manifest_path = cache.publish_composite(
        slices=(
            SongCacheSlice(first, cache_start_frame=52, frame_count=8),
            SongCacheSlice(second, cache_start_frame=0, frame_count=13),
        ),
        outbox=tmp_path / "outbox",
        sequence=77,
        latency_seconds=8.1,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cache_hit"] is True
    assert manifest["cache_scope"] == "song-composite"
    assert manifest["cache_part_count"] == 2
    assert [track["title"] for track in manifest["tracks"]] == [
        "First Song",
        "Second Song",
    ]
    with wave.open(str(tmp_path / "outbox" / manifest["stems"]["vocals"]), "rb") as audio:
        assert audio.getnframes() == 21
        assert audio.readframes(21) == (
            _pcm(range(52, 60), scale=2)
            + _pcm(range(0, 13), scale=2)
        )
