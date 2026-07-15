from __future__ import annotations

import math
import time
from collections.abc import Mapping


class LiveAcceptanceRecorder:
    """Aggregate real-phone evidence without treating idle drain as an underrun."""

    def __init__(self, *, mixer_latency_limit_ms: float = 100.0) -> None:
        if not math.isfinite(mixer_latency_limit_ms) or mixer_latency_limit_ms <= 0.0:
            raise ValueError("混音控制延迟上限必须为正数。")
        self.mixer_latency_limit_ms = float(mixer_latency_limit_ms)
        self._started_at_ns: int | None = None
        self._last_observed_at_ns: int | None = None
        self._samples = 0
        self._streaming_samples = 0
        self._playing_samples = 0
        self._active_samples = 0
        self._initial_captured_sequence = 0
        self._initial_gpu_sequence = 0
        self._initial_cache_hits = 0
        self._initial_cache_misses = 0
        self._initial_songs_cached = 0
        self._maximum_captured_sequence = 0
        self._maximum_gpu_sequence = 0
        self._maximum_cache_hits = 0
        self._maximum_cache_misses = 0
        self._maximum_songs_cached = 0
        self._latest_songs_cached = 0
        self._maximum_pending_windows = 0
        self._minimum_active_buffer_seconds: float | None = None
        self._active_started = False
        self._active_underrun_baseline = 0
        self._active_underrun_maximum = 0
        self._active_mixer_baseline = 0
        self._active_mixer_maximum = 0
        self._previous_mixer_updates = 0
        self._max_active_mixer_latency_ms = 0.0
        self._song_cache_hit_samples = 0
        self._tracks_seen: list[dict[str, object]] = []
        self._track_keys: set[tuple[str, str, float]] = set()

    @staticmethod
    def _count(snapshot: Mapping[str, object], name: str) -> int:
        try:
            return max(0, int(snapshot.get(name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _metric(snapshot: Mapping[str, object], name: str) -> float:
        try:
            value = float(snapshot.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    def observe(
        self,
        snapshot: Mapping[str, object],
        *,
        observed_at_ns: int | None = None,
    ) -> None:
        now_ns = time.time_ns() if observed_at_ns is None else int(observed_at_ns)
        if now_ns < 0:
            raise ValueError("验收采样时间不得为负数。")

        captured_sequence = self._count(snapshot, "captured_sequence")
        gpu_sequence = self._count(snapshot, "gpu_sequence")
        cache_hits = self._count(snapshot, "cache_hits")
        cache_misses = self._count(snapshot, "cache_misses")
        songs_cached = self._count(snapshot, "songs_cached")
        pending_windows = self._count(snapshot, "pending_windows")
        underruns = self._count(snapshot, "underruns")
        mixer_updates = self._count(snapshot, "mixer_updates")
        mixer_latency_ms = self._metric(snapshot, "last_mixer_control_latency_ms")
        buffered_seconds = self._metric(snapshot, "ready_buffer_seconds")
        streaming = snapshot.get("streaming") is True
        playing = str(snapshot.get("playback_state", "")) == "playing"
        active = streaming or playing

        if self._started_at_ns is None:
            self._started_at_ns = now_ns
            self._initial_captured_sequence = captured_sequence
            self._initial_gpu_sequence = gpu_sequence
            self._initial_cache_hits = cache_hits
            self._initial_cache_misses = cache_misses
            self._initial_songs_cached = songs_cached
            self._previous_mixer_updates = mixer_updates
        self._last_observed_at_ns = now_ns
        self._samples += 1
        self._streaming_samples += int(streaming)
        self._playing_samples += int(playing)
        self._maximum_captured_sequence = max(
            self._maximum_captured_sequence,
            captured_sequence,
        )
        self._maximum_gpu_sequence = max(self._maximum_gpu_sequence, gpu_sequence)
        self._maximum_cache_hits = max(self._maximum_cache_hits, cache_hits)
        self._maximum_cache_misses = max(self._maximum_cache_misses, cache_misses)
        self._maximum_songs_cached = max(self._maximum_songs_cached, songs_cached)
        self._latest_songs_cached = songs_cached
        self._maximum_pending_windows = max(self._maximum_pending_windows, pending_windows)

        if active:
            self._active_samples += 1
            if not self._active_started:
                self._active_started = True
                self._active_underrun_baseline = underruns
                self._active_underrun_maximum = underruns
                self._active_mixer_baseline = mixer_updates
                self._active_mixer_maximum = mixer_updates
            else:
                self._active_underrun_maximum = max(
                    self._active_underrun_maximum,
                    underruns,
                )
                self._active_mixer_maximum = max(
                    self._active_mixer_maximum,
                    mixer_updates,
                )
            if mixer_updates > self._previous_mixer_updates:
                self._max_active_mixer_latency_ms = max(
                    self._max_active_mixer_latency_ms,
                    mixer_latency_ms,
                )
            if self._minimum_active_buffer_seconds is None:
                self._minimum_active_buffer_seconds = buffered_seconds
            else:
                self._minimum_active_buffer_seconds = min(
                    self._minimum_active_buffer_seconds,
                    buffered_seconds,
                )

            title = str(snapshot.get("track_title", "") or "").strip()[:512]
            artist = str(snapshot.get("track_artist", "") or "").strip()[:512]
            duration_seconds = round(
                self._metric(snapshot, "track_duration_seconds"),
                3,
            )
            if title or artist:
                key = (title, artist, duration_seconds)
                if key not in self._track_keys:
                    self._track_keys.add(key)
                    self._tracks_seen.append(
                        {
                            "title": title,
                            "artist": artist,
                            "duration_seconds": duration_seconds,
                        }
                    )

        if (
            snapshot.get("cache_hit") is True
            and str(snapshot.get("cache_scope", "")) in {"song", "song-composite"}
            and cache_hits > self._initial_cache_hits
        ):
            self._song_cache_hit_samples += 1
        self._previous_mixer_updates = mixer_updates

    def report(self, *, observed_at_ns: int | None = None) -> dict[str, object]:
        now_ns = time.time_ns() if observed_at_ns is None else int(observed_at_ns)
        started_at_ns = self._started_at_ns or now_ns
        active_underrun_delta = max(
            0,
            self._active_underrun_maximum - self._active_underrun_baseline,
        )
        active_mixer_update_delta = max(
            0,
            self._active_mixer_maximum - self._active_mixer_baseline,
        )
        cache_hit_delta = max(0, self._maximum_cache_hits - self._initial_cache_hits)
        cache_miss_delta = max(0, self._maximum_cache_misses - self._initial_cache_misses)
        requirements = {
            "stream_received": (
                self._streaming_samples > 0
                and self._maximum_captured_sequence > self._initial_captured_sequence
            ),
            "gpu_first_play": (
                cache_miss_delta > 0
                and self._maximum_gpu_sequence > self._initial_gpu_sequence
            ),
            "song_cache_available": self._latest_songs_cached > 0,
            "song_cache_replayed": self._song_cache_hit_samples > 0,
            "zero_active_underruns": (
                self._active_samples > 0 and active_underrun_delta == 0
            ),
            "mixer_adjusted_during_stream": active_mixer_update_delta > 0,
            "mixer_latency_below_limit": (
                active_mixer_update_delta > 0
                and self._max_active_mixer_latency_ms <= self.mixer_latency_limit_ms
            ),
        }
        passed = all(requirements.values())
        terminal_failure = requirements["song_cache_replayed"] and (
            not requirements["zero_active_underruns"]
            or (
                requirements["mixer_adjusted_during_stream"]
                and not requirements["mixer_latency_below_limit"]
            )
        )
        state = (
            "passed"
            if passed
            else "failed"
            if terminal_failure
            else "waiting_for_phone"
            if not requirements["stream_received"]
            else "in_progress"
        )
        return {
            "version": 1,
            "state": state,
            "passed": passed,
            "started_at_ns": started_at_ns,
            "observed_at_ns": now_ns,
            "elapsed_seconds": round(max(0, now_ns - started_at_ns) / 1e9, 3),
            "mixer_latency_limit_ms": self.mixer_latency_limit_ms,
            "requirements": requirements,
            "metrics": {
                "samples": self._samples,
                "streaming_samples": self._streaming_samples,
                "playing_samples": self._playing_samples,
                "active_samples": self._active_samples,
                "captured_sequence_delta": max(
                    0,
                    self._maximum_captured_sequence - self._initial_captured_sequence,
                ),
                "gpu_sequence_delta": max(
                    0,
                    self._maximum_gpu_sequence - self._initial_gpu_sequence,
                ),
                "cache_hit_delta": cache_hit_delta,
                "cache_miss_delta": cache_miss_delta,
                "songs_cached_initial": self._initial_songs_cached,
                "songs_cached_maximum": self._maximum_songs_cached,
                "songs_cached_latest": self._latest_songs_cached,
                "song_cache_hit_samples": self._song_cache_hit_samples,
                "active_underrun_delta": active_underrun_delta,
                "active_mixer_update_delta": active_mixer_update_delta,
                "max_active_mixer_latency_ms": round(
                    self._max_active_mixer_latency_ms,
                    3,
                ),
                "minimum_active_buffer_seconds": (
                    round(self._minimum_active_buffer_seconds, 3)
                    if self._minimum_active_buffer_seconds is not None
                    else None
                ),
                "maximum_pending_windows": self._maximum_pending_windows,
                "tracks_seen": list(self._tracks_seen),
            },
        }
