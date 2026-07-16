from __future__ import annotations

import json
from pathlib import Path

import stemstudio.lyrics as lyrics_module
from stemstudio.lyrics import (
    LyricLine,
    LyricsCache,
    LyricsDocument,
    LyricsService,
    LyricsTrack,
    LrclibClient,
    parse_lrc,
    select_lyric_window,
)


def _track(*, revision: int = 7, title: str = "小狗") -> LyricsTrack:
    return LyricsTrack(
        revision=revision,
        title=title,
        artist="胡彦斌",
        album="小狗 - Single",
        duration_seconds=280.84,
    )


def _document(track: LyricsTrack | None = None) -> LyricsDocument:
    return LyricsDocument(
        provider="lrclib",
        provider_id=123,
        track=track or _track(),
        lines=(
            LyricLine(time_seconds=1.0, text="第一句"),
            LyricLine(time_seconds=3.5, text="第二句"),
        ),
        instrumental=False,
    )


def test_parse_lrc_expands_multiple_timestamps_applies_offset_and_sorts() -> None:
    lines = parse_lrc(
        "\n".join(
            (
                "[ar:胡彦斌]",
                "[offset:500]",
                "[00:03.50]第二句",
                "[00:01.00][00:05.25]重复句",
                "invalid line",
                "[not-a-time]ignored",
            )
        )
    )

    assert lines == (
        LyricLine(time_seconds=1.5, text="重复句"),
        LyricLine(time_seconds=4.0, text="第二句"),
        LyricLine(time_seconds=5.75, text="重复句"),
    )


def test_parse_lrc_rejects_empty_or_oversized_input() -> None:
    assert parse_lrc("") == ()
    try:
        parse_lrc("x" * 1_000_001)
    except ValueError as exc:
        assert "过大" in str(exc)
    else:
        raise AssertionError("oversized LRC was accepted")


def test_select_lyric_window_tracks_boundaries_without_guessing_before_first_line() -> None:
    lines = (
        LyricLine(1.0, "A"),
        LyricLine(2.0, "B"),
        LyricLine(3.0, "C"),
        LyricLine(4.0, "D"),
    )

    before = select_lyric_window(lines, 0.5, context=2)
    assert before.current is None
    assert before.upcoming == lines[:2]

    exact = select_lyric_window(lines, 3.0, context=1)
    assert exact.previous == (lines[1],)
    assert exact.current == lines[2]
    assert exact.upcoming == (lines[3],)

    after = select_lyric_window(lines, 99.0, context=2)
    assert after.current == lines[-1]
    assert after.previous == lines[-3:-1]
    assert after.upcoming == ()


def test_lyrics_cache_round_trip_is_atomic_and_rejects_corruption(tmp_path: Path) -> None:
    cache = LyricsCache(tmp_path)
    document = _document()

    cache.store(document)

    loaded = cache.load(document.track)
    assert loaded == document
    assert not list(tmp_path.glob("*.part"))

    cache.path_for(document.track).write_text("not json", encoding="utf-8")
    assert cache.load(document.track) is None


def test_lrclib_client_uses_exact_signature_cached_first_then_fallback() -> None:
    calls: list[str] = []

    def transport(url: str, timeout_seconds: float) -> tuple[int, bytes]:
        calls.append(url)
        assert timeout_seconds == 4.0
        if "/api/get-cached?" in url:
            return 404, b""
        if "/api/search?" in url:
            return 404, b""
        return 200, json.dumps(
            {
                "id": 123,
                "trackName": "小狗",
                "artistName": "胡彦斌",
                "albumName": "小狗 - Single",
                "duration": 281,
                "instrumental": False,
                "syncedLyrics": "[00:01.00]第一句\n[00:03.50]第二句",
            },
            ensure_ascii=False,
        ).encode("utf-8")

    document = LrclibClient(transport=transport, timeout_seconds=4.0).fetch(_track())

    assert document is not None
    assert [line.text for line in document.lines] == ["第一句", "第二句"]
    assert len(calls) == 3
    assert "/api/get-cached?" in calls[0]
    assert "/api/search?" in calls[1]
    assert "/api/get?" in calls[2]
    assert "track_name=%E5%B0%8F%E7%8B%97" in calls[0]
    assert "artist_name=%E8%83%A1%E5%BD%A6%E6%96%8C" in calls[0]
    assert "duration=281" in calls[0]


def test_lrclib_client_treats_instrumental_or_missing_synced_lyrics_explicitly() -> None:
    payload = json.dumps(
        {
            "id": 9,
            "trackName": "Instrumental",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 60,
            "instrumental": True,
            "syncedLyrics": None,
        }
    ).encode()
    client = LrclibClient(transport=lambda _url, _timeout: (200, payload))
    track = LyricsTrack(1, "Instrumental", "Artist", "Album", 60.0)

    document = client.fetch(track)

    assert document is not None
    assert document.instrumental is True
    assert document.lines == ()


def test_lrclib_client_search_fallback_requires_exact_identity_and_close_duration() -> None:
    calls: list[str] = []

    def transport(url: str, _timeout_seconds: float) -> tuple[int, bytes]:
        calls.append(url)
        if "/api/search?" not in url:
            return 404, b""
        return 200, json.dumps(
            [
                {
                    "id": 1,
                    "trackName": "小狗 (Live)",
                    "artistName": "胡彦斌",
                    "albumName": "Live",
                    "duration": 280.84,
                    "syncedLyrics": "[00:01.00]错误版本",
                },
                {
                    "id": 2,
                    "trackName": "小狗",
                    "artistName": "胡彦斌",
                    "albumName": "Anson Hu",
                    "duration": 350.0,
                    "syncedLyrics": "[00:01.00]错误时长",
                },
                {
                    "id": 3,
                    "trackName": "小狗",
                    "artistName": "胡彦斌",
                    "albumName": "Anson Hu",
                    "duration": 280.826,
                    "syncedLyrics": "[00:01.00]正确歌词",
                },
            ],
            ensure_ascii=False,
        ).encode("utf-8")

    document = LrclibClient(transport=transport).fetch(_track())

    assert document is not None
    assert document.provider_id == 3
    assert [line.text for line in document.lines] == ["正确歌词"]
    assert len(calls) == 2
    assert "/api/get-cached?" in calls[0]
    assert "/api/search?" in calls[1]
    assert "album_name" not in calls[1]


def test_lyrics_service_fetches_off_audio_thread_then_reuses_persistent_cache(
    tmp_path: Path,
) -> None:
    live_root = tmp_path / "live"
    cache_root = tmp_path / "lyrics"
    live_root.mkdir()
    (live_root / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "track": {
                    "revision": 7,
                    "title": "小狗",
                    "artist": "胡彦斌",
                    "album": "小狗 - Single",
                    "duration_seconds": 280.84,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FetchOnce:
        calls = 0

        def fetch(self, track: LyricsTrack) -> LyricsDocument:
            self.calls += 1
            return _document(track)

    client = FetchOnce()
    LyricsService(live_root, cache_root, client=client).refresh()
    first = json.loads((live_root / "lyrics-status.json").read_text(encoding="utf-8"))
    assert first["state"] == "ready"
    assert first["source"] == "lrclib"
    assert first["track"]["revision"] == 7
    assert first["lines"][1] == {"time_seconds": 3.5, "text": "第二句"}
    assert client.calls == 1

    class MustNotFetch:
        def fetch(self, _track: LyricsTrack) -> LyricsDocument:
            raise AssertionError("persistent lyrics cache was not reused")

    LyricsService(live_root, cache_root, client=MustNotFetch()).refresh()
    cached = json.loads((live_root / "lyrics-status.json").read_text(encoding="utf-8"))
    assert cached["state"] == "ready"
    assert cached["source"] == "cache"


def test_lyrics_service_clears_stale_song_and_contains_network_failure(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    (live_root / "lyrics-status.json").write_text(
        json.dumps({"state": "ready", "track": {"title": "Old"}}),
        encoding="utf-8",
    )
    (live_root / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "track": {
                    "revision": 8,
                    "title": "New Song",
                    "artist": "Artist",
                    "album": "Album",
                    "duration_seconds": 120,
                },
            }
        ),
        encoding="utf-8",
    )

    class BrokenClient:
        def fetch(self, _track: LyricsTrack) -> None:
            raise TimeoutError("offline")

    LyricsService(live_root, tmp_path / "lyrics", client=BrokenClient()).refresh()

    status = json.loads((live_root / "lyrics-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["track"]["title"] == "New Song"
    assert "offline" in status["error"]
    assert status["lines"] == []


def test_lyrics_service_rate_limits_errors_then_retries_current_track(
    tmp_path: Path, monkeypatch
) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    (live_root / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "track": {
                    "revision": 9,
                    "title": "Retry Song",
                    "artist": "Artist",
                    "duration_seconds": 90,
                },
            }
        ),
        encoding="utf-8",
    )
    clock = [100.0]
    monkeypatch.setattr(lyrics_module.time, "monotonic", lambda: clock[0])

    class Recovers:
        calls = 0

        def fetch(self, track: LyricsTrack) -> LyricsDocument:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("offline")
            return _document(track)

    client = Recovers()
    service = LyricsService(live_root, tmp_path / "lyrics", client=client)

    assert service.refresh()["state"] == "error"
    assert service.refresh()["state"] == "error"
    assert client.calls == 1
    clock[0] += 60.0
    assert service.refresh()["state"] == "ready"
    assert client.calls == 2
