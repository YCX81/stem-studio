from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_MAX_LRC_BYTES = 1_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_TIMESTAMP_PATTERN = re.compile(
    r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]?\d(?:\.\d{1,3})?)\]"
)
_OFFSET_PATTERN = re.compile(r"^\[offset:(?P<milliseconds>[+-]?\d+)\]$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LyricLine:
    time_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class LyricSelection:
    previous: tuple[LyricLine, ...]
    current: LyricLine | None
    upcoming: tuple[LyricLine, ...]


@dataclass(frozen=True, slots=True)
class LyricsTrack:
    revision: int
    title: str
    artist: str
    album: str
    duration_seconds: float

    @classmethod
    def from_airplay_status(cls, payload: object) -> LyricsTrack | None:
        if not isinstance(payload, dict) or payload.get("state") not in {
            "streaming",
            "paused",
        }:
            return None
        raw_track = payload.get("track")
        if not isinstance(raw_track, dict):
            return None
        try:
            revision = max(0, int(raw_track.get("revision", 0) or 0))
            duration = float(raw_track.get("duration_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        title = str(raw_track.get("title", "") or "").strip()
        artist = str(raw_track.get("artist", "") or "").strip()
        album = str(raw_track.get("album", "") or "").strip()
        if not title or not artist or not 0.0 < duration < 24 * 60 * 60:
            return None
        return cls(revision, title[:512], artist[:512], album[:512], duration)

    def cache_key(self) -> str:
        normalized = {
            "title": _normalize_identity_text(self.title),
            "artist": _normalize_identity_text(self.artist),
            "album": _normalize_identity_text(self.album),
            "duration": _rounded_duration(self.duration_seconds),
        }
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LyricsDocument:
    provider: str
    provider_id: int
    track: LyricsTrack
    lines: tuple[LyricLine, ...]
    instrumental: bool


class LyricsClient(Protocol):
    def fetch(self, track: LyricsTrack) -> LyricsDocument | None: ...


def _normalize_identity_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _rounded_duration(value: float) -> int:
    return max(1, int(value + 0.5))


def parse_lrc(contents: str) -> tuple[LyricLine, ...]:
    if len(contents.encode("utf-8")) > _MAX_LRC_BYTES:
        raise ValueError("LRC 歌词文件过大。")
    if not contents.strip():
        return ()

    offset_seconds = 0.0
    for raw_line in contents.splitlines():
        match = _OFFSET_PATTERN.fullmatch(raw_line.strip())
        if match:
            offset_seconds = int(match.group("milliseconds")) / 1_000.0

    lines: list[LyricLine] = []
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        timestamps = list(_TIMESTAMP_PATTERN.finditer(line))
        if not timestamps or timestamps[0].start() != 0:
            continue
        text = line[timestamps[-1].end() :].strip()
        if not text:
            continue
        for timestamp in timestamps:
            minutes = int(timestamp.group("minutes"))
            seconds = float(timestamp.group("seconds"))
            time_seconds = max(0.0, minutes * 60.0 + seconds + offset_seconds)
            lines.append(LyricLine(round(time_seconds, 3), text[:4_096]))
    return tuple(sorted(lines, key=lambda item: item.time_seconds))


def select_lyric_window(
    lines: Sequence[LyricLine],
    position_seconds: float,
    *,
    context: int = 2,
) -> LyricSelection:
    if context < 0:
        raise ValueError("歌词上下文行数不能为负数。")
    position = max(0.0, float(position_seconds))
    line_times = [line.time_seconds for line in lines]
    current_index = bisect.bisect_right(line_times, position) - 1
    if current_index < 0:
        return LyricSelection((), None, tuple(lines[:context]))
    return LyricSelection(
        tuple(lines[max(0, current_index - context) : current_index]),
        lines[current_index],
        tuple(lines[current_index + 1 : current_index + 1 + context]),
    )


class LyricsCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, track: LyricsTrack) -> Path:
        return self.root / f"{track.cache_key()}.json"

    def load(self, track: LyricsTrack) -> LyricsDocument | None:
        path = self.path_for(track)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            stored_track = LyricsTrack(
                revision=int(payload["track"].get("revision", track.revision)),
                title=str(payload["track"]["title"]),
                artist=str(payload["track"]["artist"]),
                album=str(payload["track"].get("album", "")),
                duration_seconds=float(payload["track"]["duration_seconds"]),
            )
            if int(payload.get("version", 0)) != 1 or stored_track.cache_key() != track.cache_key():
                return None
            raw_lines = payload.get("lines", [])
            if not isinstance(raw_lines, list) or len(raw_lines) > 20_000:
                return None
            lines = tuple(
                LyricLine(float(item["time_seconds"]), str(item["text"])[:4_096])
                for item in raw_lines
            )
            return LyricsDocument(
                provider=str(payload.get("provider", "unknown"))[:64],
                provider_id=max(0, int(payload.get("provider_id", 0) or 0)),
                track=LyricsTrack(
                    revision=track.revision,
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                    duration_seconds=track.duration_seconds,
                ),
                lines=lines,
                instrumental=payload.get("instrumental") is True,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def store(self, document: LyricsDocument) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(document.track)
        partial = destination.with_suffix(".json.part")
        payload = {
            "version": 1,
            "provider": document.provider,
            "provider_id": document.provider_id,
            "instrumental": document.instrumental,
            "track": {
                "revision": document.track.revision,
                "title": document.track.title,
                "artist": document.track.artist,
                "album": document.track.album,
                "duration_seconds": document.track.duration_seconds,
            },
            "lines": [
                {"time_seconds": line.time_seconds, "text": line.text}
                for line in document.lines
            ],
        }
        partial.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, destination)


LyricsTransport = Callable[[str, float], tuple[int, bytes]]


def _default_transport(url: str, timeout_seconds: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "StemStudio/0.1 (https://github.com/YCX81/stem-studio)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAX_RESPONSE_BYTES + 1)


class LrclibClient:
    def __init__(
        self,
        *,
        transport: LyricsTransport = _default_transport,
        timeout_seconds: float = 10.0,
        base_url: str = "https://lrclib.net",
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("歌词查询超时必须为正数。")
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _decode_json(body: bytes) -> object:
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("LRCLIB 响应过大。")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("LRCLIB 返回了无效 JSON。") from exc

    @staticmethod
    def _document_from_payload(
        payload: dict[str, object], track: LyricsTrack
    ) -> LyricsDocument | None:
        instrumental = payload.get("instrumental") is True
        synced = payload.get("syncedLyrics")
        lines = parse_lrc(synced) if isinstance(synced, str) else ()
        if not instrumental and not lines:
            return None
        try:
            provider_id = max(0, int(payload.get("id", 0) or 0))
        except (TypeError, ValueError):
            provider_id = 0
        return LyricsDocument(
            provider="lrclib",
            provider_id=provider_id,
            track=track,
            lines=lines,
            instrumental=instrumental,
        )

    def fetch(self, track: LyricsTrack) -> LyricsDocument | None:
        query = urllib.parse.urlencode(
            {
                "track_name": track.title,
                "artist_name": track.artist,
                "album_name": track.album,
                "duration": _rounded_duration(track.duration_seconds),
            }
        )
        status, body = self.transport(
            f"{self.base_url}/api/get-cached?{query}", self.timeout_seconds
        )
        if status == 200:
            payload = self._decode_json(body)
            if not isinstance(payload, dict):
                raise ValueError("LRCLIB 歌词响应格式无效。")
            document = self._document_from_payload(payload, track)
            if document is not None:
                return document
        elif status != 404:
            raise RuntimeError(f"LRCLIB 返回 HTTP {status}")

        search_query = urllib.parse.urlencode(
            {"track_name": track.title, "artist_name": track.artist}
        )
        status, body = self.transport(
            f"{self.base_url}/api/search?{search_query}", self.timeout_seconds
        )
        if status == 404:
            payload: object = []
        elif status != 200:
            raise RuntimeError(f"LRCLIB 返回 HTTP {status}")
        else:
            payload = self._decode_json(body)
        if not isinstance(payload, list):
            raise ValueError("LRCLIB 搜索响应格式无效。")

        candidates: list[tuple[bool, float, dict[str, object]]] = []
        for raw_candidate in payload[:1_000]:
            if not isinstance(raw_candidate, dict):
                continue
            if (
                _normalize_identity_text(str(raw_candidate.get("trackName", "")))
                != _normalize_identity_text(track.title)
                or _normalize_identity_text(str(raw_candidate.get("artistName", "")))
                != _normalize_identity_text(track.artist)
            ):
                continue
            try:
                candidate_duration = float(raw_candidate.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            duration_delta = abs(candidate_duration - track.duration_seconds)
            if not math.isfinite(candidate_duration) or duration_delta > 5.0:
                continue
            album_matches = (
                bool(track.album)
                and _normalize_identity_text(str(raw_candidate.get("albumName", "")))
                == _normalize_identity_text(track.album)
            )
            candidates.append((album_matches, duration_delta, raw_candidate))

        candidates.sort(key=lambda item: (not item[0], item[1]))
        for _album_matches, _duration_delta, candidate in candidates:
            document = self._document_from_payload(candidate, track)
            if document is not None:
                return document

        status, body = self.transport(
            f"{self.base_url}/api/get?{query}", self.timeout_seconds
        )
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"LRCLIB 返回 HTTP {status}")
        payload = self._decode_json(body)
        if not isinstance(payload, dict):
            raise ValueError("LRCLIB 歌词响应格式无效。")
        document = self._document_from_payload(payload, track)
        if document is not None:
            return document
        return None


class LyricsService:
    def __init__(
        self,
        live_root: str | Path,
        cache_root: str | Path,
        *,
        client: LyricsClient | None = None,
        retry_seconds: float = 60.0,
    ) -> None:
        if retry_seconds <= 0.0:
            raise ValueError("歌词查询重试间隔必须为正数。")
        self.live_root = Path(live_root)
        self.cache = LyricsCache(cache_root)
        self.client = client or LrclibClient()
        self.retry_seconds = float(retry_seconds)
        self.status_path = self.live_root / "lyrics-status.json"
        self._last_track_token = ""
        self._retry_track_token = ""
        self._next_retry_monotonic = 0.0

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_status(self, payload: dict[str, object]) -> dict[str, object]:
        self.live_root.mkdir(parents=True, exist_ok=True)
        partial = self.status_path.with_suffix(".json.part")
        complete = {
            "version": 1,
            **payload,
            "updated_at_ns": time.time_ns(),
        }
        partial.write_text(
            json.dumps(complete, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, self.status_path)
        return complete

    @staticmethod
    def _track_payload(track: LyricsTrack) -> dict[str, object]:
        return {
            "revision": track.revision,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration_seconds": track.duration_seconds,
        }

    def refresh(self) -> dict[str, object]:
        airplay = self._read_json(self.live_root / "airplay-status.json")
        track = LyricsTrack.from_airplay_status(airplay)
        if track is None:
            self._last_track_token = ""
            self._retry_track_token = ""
            current = self._read_json(self.status_path)
            if current.get("state") == "waiting":
                return current
            return self._write_status(
                {"state": "waiting", "source": "none", "track": {}, "lines": []}
            )

        track_token = f"{track.revision}:{track.cache_key()}"
        if track_token == self._last_track_token and self.status_path.is_file():
            retry_due = (
                track_token == self._retry_track_token
                and time.monotonic() >= self._next_retry_monotonic
            )
            if not retry_due:
                return self._read_json(self.status_path)
        self._last_track_token = track_token
        base_status: dict[str, object] = {
            "track": self._track_payload(track),
            "lines": [],
        }
        try:
            document = self.cache.load(track)
            source = "cache"
            if document is None:
                document = self.client.fetch(track)
                source = "lrclib"
                if document is not None:
                    self.cache.store(document)
            if document is None:
                self._retry_track_token = ""
                return self._write_status(
                    {**base_status, "state": "not_found", "source": "none"}
                )
            self._retry_track_token = ""
            return self._write_status(
                {
                    **base_status,
                    "state": "ready",
                    "source": source,
                    "provider": document.provider,
                    "provider_id": document.provider_id,
                    "instrumental": document.instrumental,
                    "lines": [
                        {"time_seconds": line.time_seconds, "text": line.text}
                        for line in document.lines
                    ],
                }
            )
        except Exception as exc:
            self._retry_track_token = track_token
            self._next_retry_monotonic = time.monotonic() + self.retry_seconds
            return self._write_status(
                {
                    **base_status,
                    "state": "error",
                    "source": "none",
                    "error": str(exc).strip() or type(exc).__name__,
                }
            )

    def run(self, stop_event: threading.Event, *, poll_seconds: float = 0.5) -> None:
        if poll_seconds <= 0.0:
            raise ValueError("歌词轮询间隔必须为正数。")
        while not stop_event.is_set():
            try:
                self.refresh()
            except OSError:
                pass
            stop_event.wait(poll_seconds)


def start_lyrics_service(
    live_root: str | Path,
    cache_root: str | Path,
    *,
    poll_seconds: float = 0.5,
) -> tuple[threading.Thread, threading.Event]:
    service = LyricsService(live_root, cache_root)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=service.run,
        args=(stop_event,),
        kwargs={"poll_seconds": poll_seconds},
        name="lyrics-service",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
