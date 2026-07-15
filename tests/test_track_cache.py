import json
import os
import wave
from pathlib import Path

from stemstudio.track_cache import (
    TrackCache,
    TrackCacheSpec,
    pcm_content_sha256,
)


def _write_audio(path: Path, frames: int = 80, sample: bytes = b"\x01\x00\xff\xff") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(10)
        audio.writeframes(sample * frames)


def _spec(audio_identity: str, *, six_tracks: bool = False) -> TrackCacheSpec:
    stems = (
        ("vocals", "drums", "bass", "guitar", "piano", "other")
        if six_tracks
        else ("vocals", "instrumental")
    )
    return TrackCacheSpec(
        audio_identity=audio_identity,
        profile_name="六轨 · 加吉他/钢琴" if six_tracks else "人声 / 伴奏 · 高质量",
        model_filename="htdemucs_6s.yaml" if six_tracks else "roformer.ckpt",
        stems=stems,
        sample_rate=10,
        channels=2,
        bits_per_sample=16,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
        overlap_frames=1,
    )


def _store_complete_entry(
    cache: TrackCache,
    spec: TrackCacheSpec,
    source_root: Path,
    *,
    chunks: int = 1,
) -> None:
    for chunk_index in range(chunks):
        stems = {}
        for stem in spec.stems:
            path = source_root / f"{chunk_index}-{stem}.wav"
            _write_audio(path, sample=(chunk_index + 1).to_bytes(2, "little") * 2)
            stems[stem] = path
        cache.store_chunk(spec, chunk_index, stems)
    cache.finalize(spec, chunk_count=chunks, metadata={"title": "Same Song", "artist": "Artist"})


def test_pcm_identity_is_path_independent_and_changes_with_audio(tmp_path: Path) -> None:
    first = tmp_path / "one" / "capture.wav"
    second = tmp_path / "two" / "renamed.wav"
    changed = tmp_path / "three" / "capture.wav"
    _write_audio(first)
    _write_audio(second)
    _write_audio(changed, sample=b"\x02\x00\xfe\xff")

    assert pcm_content_sha256(first) == pcm_content_sha256(second)
    assert pcm_content_sha256(first) != pcm_content_sha256(changed)


def test_cache_key_isolates_track_profile_model_and_geometry(tmp_path: Path) -> None:
    source = tmp_path / "capture.wav"
    _write_audio(source)
    identity = pcm_content_sha256(source)
    two_track = _spec(identity)
    six_track = _spec(identity, six_tracks=True)

    assert two_track.cache_key != six_track.cache_key
    assert TrackCacheSpec(**{**two_track.to_dict(), "model_filename": "new-model.ckpt"}).cache_key != two_track.cache_key
    assert TrackCacheSpec(**{**two_track.to_dict(), "hop_seconds": 1}).cache_key != two_track.cache_key
    assert TrackCacheSpec(**{**two_track.to_dict(), "stable_offset_seconds": 2}).cache_key != two_track.cache_key


def test_incomplete_or_corrupt_cache_is_a_miss(tmp_path: Path) -> None:
    cache = TrackCache(tmp_path / "cache")
    source = tmp_path / "capture.wav"
    _write_audio(source)
    spec = _spec(pcm_content_sha256(source))
    stem_paths = {}
    for stem in spec.stems:
        stem_path = tmp_path / f"{stem}.wav"
        _write_audio(stem_path)
        stem_paths[stem] = stem_path

    cache.store_chunk(spec, 0, stem_paths)
    assert cache.lookup(spec) is None

    cache.finalize(spec, chunk_count=1)
    entry = cache.lookup(spec)
    assert entry is not None
    entry.chunk_path(0, "vocals").write_bytes(b"corrupt")
    assert cache.lookup(spec) is None
    assert not entry.root.exists()

    cache.store_chunk(spec, 0, stem_paths)
    cache.finalize(spec, chunk_count=1)
    assert cache.lookup(spec) is not None


def test_complete_cache_materializes_compatible_live_result_atomically(tmp_path: Path) -> None:
    cache = TrackCache(tmp_path / "cache")
    capture = tmp_path / "capture.wav"
    _write_audio(capture)
    spec = _spec(pcm_content_sha256(capture), six_tracks=True)
    _store_complete_entry(cache, spec, tmp_path / "source", chunks=2)

    entry = cache.lookup(spec)
    assert entry is not None
    manifest_path = entry.publish_chunk(chunk_index=1, outbox=tmp_path / "outbox", sequence=42)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["sequence"] == 42
    assert payload["cache_hit"] is True
    assert payload["cache_scope"] == "window"
    assert payload["cache_key"] == spec.cache_key
    assert set(payload["stems"]) == set(spec.stems)
    assert all((tmp_path / "outbox" / filename).is_file() for filename in payload["stems"].values())
    assert not list((tmp_path / "outbox").glob("*.part"))


def test_quota_prunes_least_recently_used_complete_entry(tmp_path: Path) -> None:
    cache = TrackCache(tmp_path / "cache")
    first_capture = tmp_path / "first.wav"
    second_capture = tmp_path / "second.wav"
    _write_audio(first_capture, sample=b"\x01\x00\x01\x00")
    _write_audio(second_capture, sample=b"\x02\x00\x02\x00")
    first = _spec(pcm_content_sha256(first_capture))
    second = _spec(pcm_content_sha256(second_capture))
    _store_complete_entry(cache, first, tmp_path / "first-source")
    _store_complete_entry(cache, second, tmp_path / "second-source")

    first_entry = cache.lookup(first)
    assert first_entry is not None
    old = 1_000_000_000
    os.utime(first_entry.access_path, ns=(old, old))
    second_entry = cache.lookup(second)
    assert second_entry is not None
    newest = 2_000_000_000
    os.utime(second_entry.access_path, ns=(newest, newest))
    quota = second_entry.size_bytes + 1

    removed = cache.prune_to_quota(quota)

    assert removed == [first.cache_key]
    assert cache.lookup(first) is None
    assert cache.lookup(second) is not None
