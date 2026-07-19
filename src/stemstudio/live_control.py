from __future__ import annotations

import json
import html
import ipaddress
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .core import LIVE_PROFILES
from .lyrics import LyricLine, select_lyric_window


STEM_LABELS = {
    "vocals": "人声",
    "instrumental": "伴奏（去人声）",
    "drums": "鼓",
    "bass": "贝斯",
    "guitar": "吉他",
    "piano": "钢琴",
    "other": "其他",
}

_CAPTURE_PATTERN = re.compile(r"^capture-(\d{8})\.wav$")
_RESULT_PATTERN = re.compile(r"^result-(\d{8})\.json$")
_COMMAND_WRITE_LOCK = threading.Lock()
_MIXER_WRITE_LOCK = threading.Lock()
_ATOMIC_REPLACE_ATTEMPTS = 50
_ATOMIC_REPLACE_RETRY_SECONDS = 0.002
_STATUS_READ_ATTEMPTS = 5
_AIRPLAY_STREAM_STALE_SECONDS = 2.0
_CONTROLLER_HEARTBEAT_STALE_SECONDS = 6.0


def _normalize_device_endpoint(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    address_text, separator, port_text = candidate.rpartition(":")
    try:
        address = ipaddress.IPv4Address(address_text)
        port = int(port_text)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError("黄山派设备地址必须使用 IPv4:UDP端口。") from exc
    if not separator or not 1 <= port <= 65_535 or str(port) != port_text:
        raise ValueError("黄山派设备地址必须使用 IPv4:UDP端口。")
    return f"{address}:{port}"


def _replace_with_sharing_retry(source: Path, destination: Path) -> None:
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 == _ATOMIC_REPLACE_ATTEMPTS:
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)


def _read_status(path: Path) -> dict:
    for attempt in range(_STATUS_READ_ATTEMPTS):
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        if attempt + 1 < _STATUS_READ_ATTEMPTS:
            time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)
    return {}


def _status_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _maximum_sequence(directory: Path, pattern: re.Pattern[str]) -> int:
    if not directory.is_dir():
        return 0
    sequences = []
    for path in directory.iterdir():
        match = pattern.fullmatch(path.name)
        if match:
            sequences.append(int(match.group(1)))
    return max(sequences, default=0)


def live_ui_defaults(live_root: str | Path) -> dict:
    """Restore UI controls from the active host instead of stale browser defaults."""
    root = Path(live_root)
    controller = _read_status(root / "controller-status.json")
    state = str(controller.get("state", ""))
    controller_heartbeat_age_seconds = _status_age_seconds(
        root / "controller-heartbeat.json"
    )
    controller_online = (
        controller_heartbeat_age_seconds is not None
        and controller_heartbeat_age_seconds <= _CONTROLLER_HEARTBEAT_STALE_SECONDS
    )
    input_source = "process"
    process_id: int | None = 0
    profile_name = "人声 / 伴奏 · 高质量"
    device_endpoint = ""

    if state == "airplay_waiting" and controller_online:
        input_source = "airplay"
        process_id = None
        candidate = controller.get("profile_name")
        if candidate in LIVE_PROFILES:
            profile_name = str(candidate)
        try:
            device_endpoint = _normalize_device_endpoint(
                controller.get("device_endpoint", "")
            )
        except ValueError:
            device_endpoint = ""
    elif state == "capturing" and controller_online:
        try:
            process_id = max(0, int(controller.get("process_id", 0) or 0))
        except (TypeError, ValueError):
            process_id = 0
        candidate = controller.get("profile_name")
        if candidate in LIVE_PROFILES:
            profile_name = str(candidate)
        try:
            device_endpoint = _normalize_device_endpoint(
                controller.get("device_endpoint", "")
            )
        except ValueError:
            device_endpoint = ""
    elif not state:
        command = _read_status(root / "command.json")
        if command.get("action") in {"start", "start_airplay"}:
            input_source = (
                "airplay" if command.get("action") == "start_airplay" else "process"
            )
            if input_source == "airplay":
                process_id = None
            else:
                try:
                    process_id = max(0, int(command.get("process_id", 0) or 0))
                except (TypeError, ValueError):
                    process_id = 0
            candidate = command.get("profile_name")
            if candidate in LIVE_PROFILES:
                profile_name = str(candidate)
            try:
                device_endpoint = _normalize_device_endpoint(
                    command.get("device_endpoint", "")
                )
            except ValueError:
                device_endpoint = ""

    gains = {stem: 1.0 for stem in STEM_LABELS}
    playback = _read_status(root / "playback-status.json")
    raw_gains = playback.get("gains", {})
    if isinstance(raw_gains, dict):
        for stem, raw_gain in raw_gains.items():
            if stem not in gains:
                continue
            try:
                gain = float(raw_gain)
            except (TypeError, ValueError):
                continue
            if math.isfinite(gain):
                gains[stem] = min(1.0, max(0.0, gain))
    return {
        "input_source": input_source,
        "process_id": process_id,
        "profile_name": profile_name,
        "device_endpoint": device_endpoint,
        "gains": gains,
    }


def live_pipeline_snapshot(live_root: str | Path) -> dict:
    root = Path(live_root)
    controller = _read_status(root / "controller-status.json")
    controller_state = str(controller.get("state", ""))
    controller_heartbeat_age_seconds = _status_age_seconds(
        root / "controller-heartbeat.json"
    )
    controller_online = (
        controller_heartbeat_age_seconds is not None
        and controller_heartbeat_age_seconds <= _CONTROLLER_HEARTBEAT_STALE_SECONDS
    )
    controller_stalled = (
        controller_state in {"airplay_waiting", "capturing"}
        and not controller_online
    )
    airplay_path = root / "airplay-status.json"
    airplay = _read_status(airplay_path)
    airplay_status_age_seconds = _status_age_seconds(airplay_path)
    capture_input_path = root / "capture-input-status.json"
    capture_input = _read_status(capture_input_path)
    capture_input_age_seconds = _status_age_seconds(capture_input_path)
    windows_capture = controller_state == "capturing"
    input_status = capture_input if windows_capture else airplay
    input_status_age_seconds = (
        capture_input_age_seconds if windows_capture else airplay_status_age_seconds
    )
    try:
        source_sessions_isolated = max(
            0, int(input_status.get("source_sessions_isolated", 0) or 0)
        )
    except (TypeError, ValueError):
        source_sessions_isolated = 0
    try:
        source_isolation_db = float(input_status.get("source_isolation_db", 0.0) or 0.0)
    except (TypeError, ValueError):
        source_isolation_db = 0.0
    if not math.isfinite(source_isolation_db):
        source_isolation_db = 0.0
    raw_streaming = (
        capture_input.get("state") == "capturing"
        if windows_capture
        else airplay.get("state") == "streaming"
    )
    streaming = raw_streaming and (
        input_status_age_seconds is not None
        and input_status_age_seconds <= _AIRPLAY_STREAM_STALE_SECONDS
    )
    stream_stalled = raw_streaming and not streaming
    gpu = _read_status(root / "gpu-status.json")
    playback = _read_status(root / "playback-status.json")
    lyrics = _read_status(root / "lyrics-status.json")
    acceptance = _read_status(root / "acceptance-report.json")
    acceptance_states = {
        "waiting_for_phone",
        "in_progress",
        "passed",
        "failed",
        "timed_out",
        "stopped",
    }
    acceptance_state = str(acceptance.get("state", "not_running"))
    if acceptance_state not in acceptance_states:
        acceptance_state = "not_running"
    acceptance_requirement_names = (
        "stream_received",
        "gpu_first_play",
        "song_cache_available",
        "song_cache_replayed",
        "zero_active_underruns",
        "zero_active_device_recoveries",
        "zero_active_skipped_sequences",
        "mixer_adjusted_during_stream",
        "mixer_latency_below_limit",
    )
    raw_acceptance_requirements = acceptance.get("requirements", {})
    if not isinstance(raw_acceptance_requirements, dict):
        raw_acceptance_requirements = {}
    acceptance_requirements = {
        name: raw_acceptance_requirements.get(name) is True
        for name in acceptance_requirement_names
    }
    raw_acceptance_metrics = acceptance.get("metrics", {})
    if not isinstance(raw_acceptance_metrics, dict):
        raw_acceptance_metrics = {}

    def acceptance_count(name: str) -> int:
        try:
            return max(0, int(raw_acceptance_metrics.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    captured_sequence = _maximum_sequence(root / "inbox", _CAPTURE_PATTERN)
    gpu_sequence = max(0, int(gpu.get("last_sequence", 0) or 0))
    playback_sequence = max(0, int(playback.get("sequence", 0) or 0))
    processing_seconds = None
    latest_manifest = root / "outbox" / f"result-{gpu_sequence:08d}.json"
    manifest = _read_status(latest_manifest)
    if manifest.get("processing_seconds") is not None:
        processing_seconds = max(0.0, float(manifest["processing_seconds"]))
    cache_hit = manifest.get("cache_hit") is True
    cache_scope = str(manifest.get("cache_scope", "window"))
    if cache_scope not in {"song", "song-composite", "window"}:
        cache_scope = "window"
    cache_key = str(manifest.get("cache_key", ""))
    fallback_audio = manifest.get("fallback_audio") is True
    fallback_stem = str(manifest.get("fallback_stem", ""))
    if fallback_stem not in STEM_LABELS:
        fallback_stem = ""
    fallback_error = str(manifest.get("error", ""))[:512]

    model_state = str(gpu.get("model_state", "stopped"))
    if model_state not in {"warming", "ready", "error", "stopped"}:
        model_state = "error"

    def status_count(name: str) -> int:
        try:
            return max(0, int(gpu.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    def gpu_metric(name: str) -> float:
        try:
            value = float(gpu.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    if playback.get("state") == "playing" and playback_sequence:
        ready_windows = max(0, gpu_sequence - playback_sequence + 1)
    elif playback.get("state") == "played" and playback_sequence:
        ready_windows = max(0, gpu_sequence - playback_sequence)
    else:
        ready_windows = max(0, gpu_sequence)

    def positive_metric(name: str, default: float) -> float:
        for payload in (playback, manifest):
            try:
                value = float(payload.get(name))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0.0:
                return value
        return default

    analysis_window_seconds = positive_metric("analysis_window_seconds", 0.0)
    if analysis_window_seconds == 0.0:
        analysis_window_seconds = positive_metric("window_seconds", 12.0)
    hop_seconds = positive_metric("hop_seconds", 6.0)
    overlap_milliseconds = positive_metric("overlap_milliseconds", 0.0)
    if overlap_milliseconds == 0.0:
        overlap_frames = positive_metric("overlap_frames", 4_410.0)
        overlap_milliseconds = overlap_frames * 1_000.0 / 44_100.0

    try:
        ready_buffer_seconds = max(0.0, float(playback["buffered_seconds"]))
    except (KeyError, TypeError, ValueError):
        ready_buffer_seconds = float(ready_windows) * hop_seconds

    def nonnegative_number(name: str, default: float = 0.0) -> float:
        try:
            value = float(playback.get(name, default))
            return value if math.isfinite(value) and value >= 0.0 else default
        except (TypeError, ValueError):
            return default

    def nonnegative_integer(name: str) -> int:
        try:
            return max(0, int(playback.get(name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    gains: dict[str, float] = {}
    raw_gains = playback.get("gains", {})
    if isinstance(raw_gains, dict):
        for stem, raw_gain in raw_gains.items():
            if stem not in STEM_LABELS:
                continue
            try:
                gain = float(raw_gain)
            except (TypeError, ValueError):
                continue
            if math.isfinite(gain):
                gains[stem] = min(1.0, max(0.0, gain))

    def level(name: str) -> float:
        try:
            return min(1.0, max(0.0, float(input_status.get(name, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    waveform = []
    for value in input_status.get("waveform", [])[:64]:
        try:
            waveform.append(min(1.0, max(0.0, float(value))))
        except (TypeError, ValueError):
            waveform.append(0.0)

    raw_track = input_status.get("track", {})
    track = raw_track if isinstance(raw_track, dict) else {}
    raw_device_hresult = str(playback.get("last_device_hresult", ""))
    if re.fullmatch(r"0[xX][0-9A-Fa-f]{8}", raw_device_hresult):
        last_device_hresult = f"0x{raw_device_hresult[2:].upper()}"
    else:
        last_device_hresult = ""

    def track_text(name: str) -> str:
        value = track.get(name, "")
        return str(value)[:512] if value is not None else ""

    def track_number(name: str) -> float:
        try:
            value = float(track.get(name, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    track_revision = max(0, int(track_number("revision")))
    track_title = track_text("title")
    track_artist = track_text("artist")
    lyrics_state = "waiting"
    lyrics_source = ""
    lyrics_instrumental = False
    lyrics_error = ""
    lyrics_current = ""
    lyrics_previous: list[str] = []
    lyrics_upcoming: list[str] = []
    lyrics_line_count = 0
    lyrics_track = lyrics.get("track", {})
    if isinstance(lyrics_track, dict):
        try:
            lyrics_revision = max(0, int(lyrics_track.get("revision", 0) or 0))
        except (TypeError, ValueError):
            lyrics_revision = -1
        same_track = (
            track_revision > 0
            and lyrics_revision == track_revision
            and str(lyrics_track.get("title", "")).strip().casefold()
            == track_title.strip().casefold()
            and str(lyrics_track.get("artist", "")).strip().casefold()
            == track_artist.strip().casefold()
        )
        if same_track:
            candidate_state = str(lyrics.get("state", "waiting"))
            if candidate_state in {"ready", "not_found", "error"}:
                lyrics_state = candidate_state
            candidate_source = str(lyrics.get("source", ""))
            if candidate_source in {"cache", "lrclib"}:
                lyrics_source = candidate_source
            lyrics_instrumental = lyrics.get("instrumental") is True
            lyrics_error = str(lyrics.get("error", "") or "")[:512]

            parsed_lines: list[LyricLine] = []
            raw_lines = lyrics.get("lines", [])
            if lyrics_state == "ready" and isinstance(raw_lines, list):
                for raw_line in raw_lines[:20_000]:
                    if not isinstance(raw_line, dict):
                        continue
                    try:
                        line_time = float(raw_line.get("time_seconds", 0.0))
                    except (TypeError, ValueError):
                        continue
                    line_text = str(raw_line.get("text", "") or "").strip()[:4096]
                    if math.isfinite(line_time) and line_time >= 0.0 and line_text:
                        parsed_lines.append(LyricLine(line_time, line_text))
                parsed_lines.sort(key=lambda line: line.time_seconds)
                lyrics_line_count = len(parsed_lines)
                selection = select_lyric_window(
                    parsed_lines,
                    track_number("position_seconds"),
                    context=2,
                )
                if selection.current is not None:
                    lyrics_current = selection.current.text
                lyrics_previous = [line.text for line in selection.previous]
                lyrics_upcoming = [line.text for line in selection.upcoming]

    return {
        "controller_state": controller_state,
        "controller_online": controller_online,
        "controller_stalled": controller_stalled,
        "controller_heartbeat_age_seconds": (
            round(controller_heartbeat_age_seconds, 3)
            if controller_heartbeat_age_seconds is not None
            else None
        ),
        "streaming": streaming,
        "stream_stalled": stream_stalled,
        "input_source": "windows" if windows_capture else "airplay",
        "input_scope": str(input_status.get("input_scope", "")),
        "signal_detected": input_status.get("signal_detected") is True,
        "source_sessions_isolated": source_sessions_isolated,
        "source_isolation_db": source_isolation_db,
        "airplay_status_age_seconds": (
            round(airplay_status_age_seconds, 3)
            if airplay_status_age_seconds is not None
            else None
        ),
        "codec": (
            "PCM16 · Windows"
            if windows_capture
            else str(airplay.get("codec", "none"))
        ),
        "received_seconds": round(
            max(0, int(input_status.get("pcm_frames", 0) or 0)) / 44_100,
            1,
        ),
        "published_windows": max(0, int(airplay.get("published_windows", 0) or 0)),
        "peak_left": level("peak_left"),
        "peak_right": level("peak_right"),
        "rms_left": level("rms_left"),
        "rms_right": level("rms_right"),
        "waveform": waveform,
        "track_revision": track_revision,
        "track_title": track_title,
        "track_artist": track_artist,
        "track_album": track_text("album"),
        "track_position_seconds": round(track_number("position_seconds"), 3),
        "track_duration_seconds": round(track_number("duration_seconds"), 3),
        "lyrics_state": lyrics_state,
        "lyrics_source": lyrics_source,
        "lyrics_instrumental": lyrics_instrumental,
        "lyrics_error": lyrics_error,
        "lyrics_current": lyrics_current,
        "lyrics_previous": lyrics_previous,
        "lyrics_upcoming": lyrics_upcoming,
        "lyrics_line_count": lyrics_line_count,
        "captured_sequence": captured_sequence,
        "gpu_sequence": gpu_sequence,
        "playback_sequence": playback_sequence,
        "skipped_sequence": max(0, int(nonnegative_number("skipped_sequence"))),
        "playback_state": str(playback.get("state", "waiting")),
        "pending_windows": max(0, captured_sequence - gpu_sequence),
        "ready_buffer_seconds": round(ready_buffer_seconds, 3),
        "prebuffer_seconds": round(nonnegative_number("prebuffer_seconds"), 3),
        "underruns": max(0, int(nonnegative_number("underruns"))),
        "last_underrun_system_time_ns": nonnegative_integer(
            "last_underrun_system_time_ns"
        ),
        "last_underrun_buffer_seconds": round(
            nonnegative_integer("last_underrun_buffered_frames") / 44_100.0,
            3,
        ),
        "last_underrun_playback_seconds": round(
            nonnegative_integer("last_underrun_total_read_frames") / 44_100.0,
            3,
        ),
        "device_open_count": max(0, int(nonnegative_number("device_open_count"))),
        "device_recoveries": max(0, int(nonnegative_number("device_recoveries"))),
        "device_recovering": playback.get("device_recovering") is True,
        "last_device_hresult": last_device_hresult,
        "device_buffer_ms": round(
            nonnegative_number("device_buffer_frames") * 1_000.0 / 44_100.0,
            1,
        ),
        "device_network_enabled": playback.get("device_network_enabled") is True,
        "device_queue_packets": nonnegative_integer("device_queue_packets"),
        "device_queue_capacity_packets": nonnegative_integer(
            "device_queue_capacity_packets"
        ),
        "device_enqueued_packets": nonnegative_integer("device_enqueued_packets"),
        "device_dropped_packets": nonnegative_integer("device_dropped_packets"),
        "device_dropped_frames": nonnegative_integer("device_dropped_frames"),
        "device_sent_packets": nonnegative_integer("device_sent_packets"),
        "device_sent_bytes": nonnegative_integer("device_sent_bytes"),
        "device_send_errors": nonnegative_integer("device_send_errors"),
        "device_last_socket_error": nonnegative_integer(
            "device_last_socket_error"
        ),
        "control_sequence": max(0, int(nonnegative_number("control_sequence"))),
        "mixer_updates": max(0, int(nonnegative_number("mixer_updates"))),
        "last_mixer_control_latency_ms": round(
            nonnegative_number("last_mixer_control_latency_ms"), 3
        ),
        "max_mixer_control_latency_ms": round(
            nonnegative_number("max_mixer_control_latency_ms"), 3
        ),
        "gain_smoothing_ms": round(nonnegative_number("gain_smoothing_ms"), 3),
        "gains": gains,
        "analysis_window_seconds": round(analysis_window_seconds, 3),
        "hop_seconds": round(hop_seconds, 3),
        "overlap_milliseconds": round(overlap_milliseconds, 1),
        "processing_seconds": processing_seconds,
        "cache_hit": cache_hit,
        "cache_hits": status_count("cache_hits"),
        "cache_misses": status_count("cache_misses"),
        "songs_cached": status_count("songs_cached"),
        "fallback_audio": fallback_audio,
        "fallback_stem": fallback_stem,
        "fallback_error": fallback_error,
        "fallback_windows": status_count("fallback_windows"),
        "model_state": model_state,
        "inference_process_pid": status_count("inference_process_pid"),
        "inference_timeout_seconds": gpu_metric("inference_timeout_seconds"),
        "model_warmup_seconds": gpu_metric("model_warmup_seconds"),
        "effective_demucs_shifts": status_count("effective_demucs_shifts"),
        "shifts_benchmark_seconds": gpu_metric("shifts_benchmark_seconds"),
        "shifts_fallback": gpu.get("shifts_fallback") is True,
        "warmup_windows": status_count("warmup_windows"),
        "deadline_windows": status_count("deadline_windows"),
        "low_buffer_fallback_windows": status_count("low_buffer_fallback_windows"),
        "continuity_reserve_seconds": gpu_metric("continuity_reserve_seconds"),
        "max_processing_seconds": gpu_metric("max_processing_seconds"),
        "inference_error": str(gpu.get("inference_error", ""))[:512],
        "cache_scope": cache_scope if cache_hit else "none",
        "cache_key_short": cache_key[:12] if cache_hit else "",
        "sustainable": processing_seconds is not None and processing_seconds <= hop_seconds,
        "acceptance_state": acceptance_state,
        "acceptance_passed": (
            acceptance_state == "passed" and all(acceptance_requirements.values())
        ),
        "acceptance_requirements": acceptance_requirements,
        "acceptance_active_samples": acceptance_count("active_samples"),
        "active_underrun_delta": acceptance_count("active_underrun_delta"),
        "active_device_recovery_delta": acceptance_count(
            "active_device_recovery_delta"
        ),
        "active_skipped_sequence_delta": acceptance_count(
            "active_skipped_sequence_delta"
        ),
    }


def live_dashboard_html(live_root: str | Path) -> str:
    snapshot = live_pipeline_snapshot(live_root)
    waveform = snapshot["waveform"] or [0.0] * 32
    if len(waveform) > 40:
        waveform = waveform[::2]
    wave_clock = time.monotonic()

    def wave_bar(index: int, value: float) -> str:
        speed = 0.72 + (index % 5) * 0.09
        phase = (wave_clock + index * 0.11) % (2 * speed)
        return (
            '<i style="'
            f'--wave-low:{max(0.04, value * 0.58):.3f};'
            f'--wave-high:{max(0.08, value):.3f};'
            f'--wave-speed:{speed:.2f}s;'
            f'animation-delay:-{phase:.2f}s"></i>'
        )

    bars = "".join(wave_bar(index, value) for index, value in enumerate(waveform))
    streaming = snapshot["streaming"]
    stream_stalled = snapshot["stream_stalled"]
    controller_stalled = snapshot["controller_stalled"]
    state_class = (
        "bad"
        if controller_stalled
        else "ok"
        if streaming
        else "bad"
        if stream_stalled
        else "idle"
    )
    state_text = (
        "控制器已离线"
        if controller_stalled
        else (
            "已捕获 Windows 音频"
            if snapshot["input_source"] == "windows" and snapshot["signal_detected"]
            else "Windows 捕获已启动，当前为静音"
            if snapshot["input_source"] == "windows"
            else "正在接收音频"
        )
        if streaming
        else "上游 PCM 已停止"
        if stream_stalled
        else "等待 Windows 音频"
        if snapshot["input_source"] == "windows"
        else "等待 AirPlay 音频"
    )
    average_rms = (snapshot["rms_left"] + snapshot["rms_right"]) / 2.0
    average_peak = (snapshot["peak_left"] + snapshot["peak_right"]) / 2.0
    visual_energy = min(1.0, max(0.0, average_rms * 0.65 + average_peak * 0.35))
    if not streaming:
        visual_energy = 0.04
    energy_percent = round(visual_energy * 100)
    stereo_balance = min(
        1.0,
        max(-1.0, snapshot["rms_right"] - snapshot["rms_left"]),
    )
    orb_speed = max(1.45, 3.15 - visual_energy * 1.55)
    orb_style = (
        f"--orb-core-low:{0.90 + visual_energy * 0.08:.3f};"
        f"--orb-core-high:{1.00 + visual_energy * 0.12:.3f};"
        f"--orb-halo-low:{0.94 + visual_energy * 0.08:.3f};"
        f"--orb-halo-high:{1.08 + visual_energy * 0.18:.3f};"
        f"--orb-shift:{stereo_balance * 12.0:.1f}px;"
        f"--orb-shift-reverse:{stereo_balance * -12.0:.1f}px;"
        f"--orb-speed:{orb_speed:.2f}s;"
        f"--orb-morph-speed:{orb_speed * 1.17:.2f}s;"
        f"--orb-drift-speed:{orb_speed * 0.83:.2f}s;"
        f"--orb-ring-speed:{orb_speed * 1.80:.2f}s;"
        f"--orb-ring-secondary-speed:{orb_speed * 1.35:.2f}s;"
        f"--orb-glow:{18 + energy_percent // 2}px"
    )
    processing = snapshot["processing_seconds"]
    cache_hit_labels = {
        "song": ("整首歌曲缓存命中", "整首歌曲"),
        "song-composite": ("跨曲组合缓存命中", "跨曲组合"),
        "window": ("窗口缓存命中", "精确窗口"),
    }
    cache_hit_label, cache_scope_text = cache_hit_labels.get(
        snapshot["cache_scope"],
        cache_hit_labels["window"],
    )
    if snapshot["fallback_audio"]:
        processing_text = "分轨失败 · 原声保底已接管"
    elif snapshot["cache_hit"]:
        processing_text = f"{cache_hit_label} · 0 GPU"
    elif processing is None:
        processing_text = "—"
    else:
        processing_text = f"{processing:.2f} 秒 / {snapshot['hop_seconds']:g} 秒"
    if controller_stalled:
        continuity_class = "bad"
        age = snapshot["controller_heartbeat_age_seconds"]
        age_text = f"{age:.1f} 秒" if age is not None else "未知时长"
        continuity = (
            f"Windows 音频控制器心跳已停止 {age_text}；当前状态可能已经过期，"
            "请重新启动 Stem Studio，避免失去宿主退出检测和自动恢复。"
        )
    elif snapshot["device_recovering"]:
        continuity_class = "warm"
        device_error = html.escape(snapshot["last_device_hresult"] or "输出端点暂不可用")
        continuity = (
            "Windows 输出设备正在自动重连；已分离的多轨缓冲仍然保留，"
            f"AirPlay 接收和 GPU 缓存不会重启。最近错误：{device_error}"
        )
    elif snapshot["device_network_enabled"] and (
        snapshot["device_dropped_frames"] or snapshot["device_send_errors"]
    ):
        continuity_class = "bad"
        continuity = (
            "黄山派网络输出发生实时数据损失："
            f"丢弃 {snapshot['device_dropped_frames']} 帧，"
            f"发送错误 {snapshot['device_send_errors']} 次；"
            "真机连续性验收不能通过。"
        )
    elif stream_stalled:
        continuity_class = "bad"
        continuity = (
            "AirPlay 上游 PCM 已停止更新；手机可能已暂停、断开，或接收端停止供数。"
            "队列自然排空不计为流内欠载。"
        )
    elif snapshot["active_device_recovery_delta"]:
        continuity_class = "bad"
        continuity = (
            f"本次连续播放中声卡端点重连 {snapshot['active_device_recovery_delta']} 次，"
            "即使多轨队列未欠载也可能产生可闻断点；本次验收不会通过。"
        )
    elif snapshot["active_skipped_sequence_delta"]:
        continuity_class = "bad"
        continuity = (
            f"本次连续播放中分轨结果跳窗 {snapshot['active_skipped_sequence_delta']} 次，"
            "时间轴可能发生跳跃；本次验收不会通过。"
        )
    elif snapshot["active_underrun_delta"]:
        continuity_class = "bad"
        continuity = (
            f"本次连续播放中新增 {snapshot['active_underrun_delta']} 次流内欠载，"
            "正在自动重新预缓冲；"
            "继续增加时应降低轨数或处理窗口步长。"
        )
    elif snapshot["underruns"] and snapshot["acceptance_active_samples"]:
        continuity_class = "ok"
        continuity = (
            "本次连续播放流内欠载 0 次；"
            f"累计 {snapshot['underruns']} 次来自暂停或断流阶段的队列自然排空，"
            "不代表正在播放时卡顿。"
        )
    elif snapshot["underruns"]:
        continuity_class = "bad"
        continuity = (
            f"输出队列已发生 {snapshot['underruns']} 次欠载，正在自动重新预缓冲；"
            "继续增加时应降低轨数或处理窗口步长。"
        )
    elif snapshot["fallback_audio"]:
        continuity_class = "warm"
        fallback_reason = html.escape(snapshot["fallback_error"] or "分轨结果不可用")
        continuity = (
            "本窗已自动切换到原声保底，连续时间轴保持不变；"
            f"下一窗继续尝试恢复分轨。原因：{fallback_reason}"
        )
    elif not streaming:
        continuity_class = "idle"
        continuity = "连接手机并开始播放后，这里会显示真实输入电平和流水线进度。"
    elif processing is None:
        continuity_class = "warm"
        continuity = "正在积累首个 12 秒分析窗并预热模型，暂时还没有分离输出。"
    elif snapshot["sustainable"]:
        continuity_class = "warm"
        continuity = "算力可以跟上实时输入；多轨共用同一帧时钟，并由持久 WASAPI 输出队列连续混音播放。"
    else:
        continuity_class = "bad"
        continuity = (
            f"单窗处理超过 {snapshot['hop_seconds']:g} 秒步进期限，"
            "长期运行会耗尽缓冲并产生停顿。"
        )

    def meter(value: float) -> str:
        return f'<span><b style="width:{round(value * 100)}%"></b></span><em>{value * 100:.0f}%</em>'

    gains = snapshot["gains"]
    gain_text = " · ".join(
        f"{html.escape(STEM_LABELS[stem])} {gain * 100:.0f}%"
        for stem, gain in gains.items()
    ) or "等待混音器状态"
    song_inventory_text = (
        f"本地已有 {snapshot['songs_cached']} 首完整歌曲缓存"
        if snapshot["songs_cached"]
        else "本地暂无完整歌曲缓存"
    )
    if snapshot["fallback_audio"]:
        fallback_label = STEM_LABELS.get(snapshot["fallback_stem"], "原声")
        cache_text = (
            f"原声保底：本窗原始立体声由{fallback_label}轨连续播放，未写入分轨缓存"
            f" · 累计 {snapshot['fallback_windows']} 窗"
        )
    elif snapshot["cache_hit"]:
        cache_text = (
            f"最近一窗：本地多轨缓存命中（{cache_scope_text}），GPU 未重复分离"
            f" · 命中 {snapshot['cache_hits']} 窗 / 新处理 {snapshot['cache_misses']} 窗"
            f" · 指纹 {snapshot['cache_key_short']}"
        )
    else:
        cache_text = "内容缓存：最近一窗未命中，完成分离后会自动写入本地缓存"
    cache_text = f"{cache_text} · {song_inventory_text}"

    model_labels = {
        "warming": "模型完整预热中",
        "ready": "模型就绪",
        "error": "模型异常",
        "stopped": "模型未启动",
    }
    process_text = (
        f"PID {snapshot['inference_process_pid']}"
        if snapshot["inference_process_pid"]
        else "无推理进程"
    )
    warmup_text = (
        f"完整预热 {snapshot['model_warmup_seconds']:.2f} 秒"
        if snapshot["model_warmup_seconds"] > 0.0
        else "等待完整预热"
    )
    tuning_text = (
        f" · shifts={snapshot['effective_demucs_shifts']}"
        f" · 热推理 {snapshot['shifts_benchmark_seconds']:.2f} 秒"
        + ("（已自动回退）" if snapshot["shifts_fallback"] else "")
        if snapshot["effective_demucs_shifts"]
        else ""
    )
    model_text = (
        f"{model_labels[snapshot['model_state']]} · {process_text}"
        f" · 硬截止 {snapshot['inference_timeout_seconds']:g} 秒"
        f" · {warmup_text}"
        f"{tuning_text}"
        f" · 预热保底 {snapshot['warmup_windows']} 窗"
        f" · 超时保底 {snapshot['deadline_windows']} 窗"
        f" · 低余量保底 {snapshot['low_buffer_fallback_windows']} 窗"
        f" · 连续性安全线 {snapshot['continuity_reserve_seconds']:g} 秒"
        f" · 最慢 {snapshot['max_processing_seconds']:.2f} 秒"
    )
    underrun_event_text = ""
    if snapshot["last_underrun_system_time_ns"]:
        underrun_clock = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(snapshot["last_underrun_system_time_ns"] / 1_000_000_000),
        )
        underrun_event_text = (
            f" · 上次欠载 {underrun_clock}"
            f" · 当时队列 {snapshot['last_underrun_buffer_seconds']:g} 秒"
            f" · 播放时间轴 {snapshot['last_underrun_playback_seconds']:g} 秒"
        )

    def clock_text(seconds: float) -> str:
        whole = max(0, int(seconds))
        hours, remainder = divmod(whole, 3_600)
        minutes, second = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{second:02d}" if hours else f"{minutes:02d}:{second:02d}"

    default_title = (
        "正在捕获全部 Windows 音频"
        if snapshot["input_source"] == "windows"
        and snapshot["input_scope"] == "system"
        else "正在捕获 Windows 音频"
        if snapshot["input_source"] == "windows"
        else "等待手机发送曲目信息"
    )
    title = html.escape(snapshot["track_title"] or default_title)
    artist = html.escape(snapshot["track_artist"])
    album = html.escape(snapshot["track_album"])
    track_details = " · ".join(value for value in (artist, album) if value)
    if not track_details and snapshot["input_source"] == "windows":
        track_details = "当前播放器尚未向 Windows 媒体会话提供曲目信息"
    duration = snapshot["track_duration_seconds"]
    progress = (
        f"{clock_text(snapshot['track_position_seconds'])} / {clock_text(duration)}"
        if duration > 0.0
        else "进度待同步"
    )
    lyrics_source_labels = {"cache": "本地缓存", "lrclib": "LRCLIB"}
    lyrics_source = lyrics_source_labels.get(snapshot["lyrics_source"], "等待匹配")
    if snapshot["lyrics_state"] == "ready" and snapshot["lyrics_instrumental"]:
        lyrics_body = '<div class="stem-lyrics-empty">纯音乐 · 无需同步歌词</div>'
    elif snapshot["lyrics_state"] == "ready":
        previous_lines = "".join(
            f'<div class="stem-lyrics-context">{html.escape(line)}</div>'
            for line in snapshot["lyrics_previous"]
        )
        current_line = html.escape(snapshot["lyrics_current"] or "等待第一句…")
        upcoming_lines = "".join(
            f'<div class="stem-lyrics-context">{html.escape(line)}</div>'
            for line in snapshot["lyrics_upcoming"]
        )
        lyrics_body = (
            f"{previous_lines}"
            f'<div class="stem-lyrics-current">{current_line}</div>'
            f"{upcoming_lines}"
        )
    elif snapshot["lyrics_state"] == "not_found":
        lyrics_body = '<div class="stem-lyrics-empty">未找到这首歌的同步歌词</div>'
    elif snapshot["lyrics_state"] == "error":
        lyrics_body = '<div class="stem-lyrics-empty">歌词服务暂不可用，将在稍后重试</div>'
    else:
        lyrics_body = '<div class="stem-lyrics-empty">等待曲目信息与同步歌词…</div>'
    acceptance_labels = {
        "waiting_for_phone": "等待手机",
        "in_progress": "进行中",
        "passed": "全部通过",
        "failed": "未通过",
        "timed_out": "已超时",
        "stopped": "已停止",
        "not_running": "未启动",
    }
    acceptance_class = (
        "ok"
        if snapshot["acceptance_passed"]
        else "bad"
        if snapshot["acceptance_state"] == "failed"
        else "warm"
        if snapshot["acceptance_state"] == "in_progress"
        else "idle"
    )
    acceptance_check_labels = (
        ("stream_received", "手机音频"),
        ("gpu_first_play", "首播 GPU"),
        ("song_cache_available", "整曲缓存"),
        ("song_cache_replayed", "歌曲缓存重播"),
        ("zero_active_underruns", "播放中零欠载"),
        ("zero_active_device_recoveries", "播放中声卡零重连"),
        ("zero_active_skipped_sequences", "播放中分轨零跳窗"),
        ("mixer_adjusted_during_stream", "播放中调音"),
        ("mixer_latency_below_limit", "调音延迟 ≤ 50 ms"),
    )
    acceptance_checklist = " · ".join(
        f"{label} {'✓' if snapshot['acceptance_requirements'][name] else '○'}"
        for name, label in acceptance_check_labels
    )
    if snapshot["device_network_enabled"]:
        device_network_text = (
            "黄山派网络 · "
            f"队列 {snapshot['device_queue_packets']}/"
            f"{snapshot['device_queue_capacity_packets']} 包 · "
            f"已发送 {snapshot['device_sent_packets']} 包 · "
            f"丢弃 {snapshot['device_dropped_frames']} 帧 · "
            f"发送错误 {snapshot['device_send_errors']} 次 · "
            f"Winsock {snapshot['device_last_socket_error']}"
        )
    else:
        device_network_text = "黄山派网络未启用"

    return f"""
<div class="stem-live">
  <style>
    .stem-live{{background:#10131a;color:#eef2ff;border:1px solid #252b39;border-radius:16px;padding:18px;font-family:ui-sans-serif,system-ui}}
    .stem-live,.stem-live strong,.stem-live b{{color:#eef2ff!important}}
    .stem-head{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px}}
    .stem-head strong{{font-size:16px}} .stem-chip{{font-size:12px;border-radius:999px;padding:5px 9px;background:#2a3040}}
    .stem-chip.ok{{background:#123c2b;color:#79f2b2}} .stem-chip.idle{{color:#aab3c5}}
    .stem-track{{display:flex;justify-content:space-between;gap:12px;align-items:end;background:#171b24;border-radius:10px;padding:10px 12px;margin-bottom:12px}}
    .stem-track b{{display:block;font-size:14px}} .stem-track small{{color:#8e99ad!important}} .stem-track time{{white-space:nowrap;color:#a5b4fc!important;font-size:12px}}
    .stem-visuals{{display:grid;grid-template-columns:210px 1fr;gap:12px;align-items:stretch}}
    .stem-orb-panel,.stem-wave-panel{{position:relative;background:radial-gradient(circle at 50% 20%,#151b2b,#090b10 72%);border:1px solid #202737;border-radius:14px;overflow:hidden}}
    .stem-orb-panel{{min-height:168px;display:grid;place-items:center;padding:12px}}
    .stem-orb{{position:relative;width:126px;height:126px;isolation:isolate;transform:translateZ(0)}}
    .stem-orb span{{position:absolute;inset:0;display:block}}
    .stem-orb-halo{{border-radius:43% 57% 52% 48%/49% 43% 57% 51%;background:radial-gradient(circle at 32% 28%,rgba(103,232,249,.88),rgba(139,92,246,.62) 38%,rgba(236,72,153,.28) 60%,transparent 73%);filter:blur(9px);opacity:.8;animation:stem-orb-breathe var(--orb-speed) ease-in-out infinite;transform:translate3d(var(--orb-shift),0,0) scale(var(--orb-halo-low))}}
    .stem-orb-core{{inset:13px!important;border-radius:48% 52% 44% 56%/53% 42% 58% 47%!important;background:conic-gradient(from 210deg,#22d3ee,#6366f1 24%,#a855f7 48%,#ec4899 68%,#38bdf8 86%,#22d3ee);box-shadow:0 0 var(--orb-glow) rgba(99,102,241,.62),inset -18px -14px 28px rgba(7,9,18,.42),inset 12px 10px 24px rgba(255,255,255,.24);animation:stem-orb-morph var(--orb-morph-speed) ease-in-out infinite alternate;transform:scale(var(--orb-core-low)) rotate(-5deg)}}
    .stem-orb-sheen{{inset:24px 31px 59px 29px!important;border-radius:50%!important;background:radial-gradient(circle,rgba(255,255,255,.85),rgba(165,243,252,.2) 58%,transparent 72%);filter:blur(2px);opacity:.72;animation:stem-orb-drift var(--orb-drift-speed) ease-in-out infinite alternate}}
    .stem-orb-ring{{inset:-6px!important;border:1px solid rgba(165,180,252,.34);border-radius:46% 54% 58% 42%/44% 51% 49% 56%!important;animation:stem-orb-ring var(--orb-ring-speed) linear infinite}}
    .stem-orb-ring.secondary{{inset:5px!important;border-color:rgba(103,232,249,.2);animation-direction:reverse;animation-duration:var(--orb-ring-secondary-speed)}}
    .stem-orb-caption{{position:absolute;bottom:8px;color:#8e99ad!important;font-size:10px;letter-spacing:.06em}}
    .stem-wave-panel{{padding:12px;min-width:0}}
    .stem-wave-title{{display:flex;justify-content:space-between;color:#8e99ad!important;font-size:11px;margin-bottom:8px}}
    .stem-wave{{height:116px;display:flex;align-items:center;gap:3px;overflow:hidden}}
    .stem-wave i{{display:block;flex:1;min-width:2px;height:100%;border-radius:999px;background:linear-gradient(180deg,#67e8f9,#8b5cf6 58%,#ec4899);opacity:.9;transform-origin:center;transform:scaleY(var(--wave-low));animation:stem-wave-flow var(--wave-speed) ease-in-out infinite alternate}}
    @keyframes stem-wave-flow{{from{{transform:scaleY(var(--wave-low));opacity:.58}}to{{transform:scaleY(var(--wave-high));opacity:1}}}}
    @keyframes stem-orb-breathe{{0%,100%{{transform:translate3d(var(--orb-shift),0,0) scale(var(--orb-halo-low)) rotate(-4deg);opacity:.64}}50%{{transform:translate3d(var(--orb-shift-reverse),-2px,0) scale(var(--orb-halo-high)) rotate(7deg);opacity:.94}}}}
    @keyframes stem-orb-morph{{0%{{transform:scale(var(--orb-core-low)) rotate(-5deg)}}100%{{transform:scale(var(--orb-core-high)) rotate(7deg)}}}}
    @keyframes stem-orb-drift{{from{{transform:translate3d(-8px,-3px,0) scale(.88);opacity:.5}}to{{transform:translate3d(12px,9px,0) scale(1.08);opacity:.9}}}}
    @keyframes stem-orb-ring{{to{{transform:rotate(360deg) scale(var(--orb-halo-high))}}}}
    .stem-lyrics{{margin-top:12px;background:#171b24;border-radius:12px;padding:13px;text-align:center;min-height:96px}}
    .stem-lyrics h4{{font-size:12px;color:#aab3c5!important;margin:0 0 10px;text-align:left}}
    .stem-lyrics-context{{color:#7d879a!important;font-size:12px;line-height:1.7;white-space:pre-wrap}}
    .stem-lyrics-current{{color:#eef2ff!important;font-size:18px;font-weight:700;line-height:1.6;margin:3px 0;white-space:pre-wrap}}
    .stem-lyrics-empty{{color:#8e99ad!important;font-size:13px;padding:18px 0}}
    .stem-grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-top:14px}} .stem-card{{background:#171b24;border-radius:12px;padding:13px}}
    .stem-card h4{{font-size:12px;color:#aab3c5!important;margin:0 0 10px}} .meter{{color:#eef2ff!important;display:grid;grid-template-columns:14px 1fr 38px;gap:8px;align-items:center;margin:7px 0;font-size:12px}}
    .meter span{{height:8px;background:#2b3140;border-radius:8px;overflow:hidden}} .meter b{{display:block;height:100%;background:#22d3ee;border-radius:8px;transition:width .18s ease-out}} .meter em{{font-style:normal;color:#aab3c5!important;text-align:right}}
    .pipe{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .pipe div{{background:#0e1118;border-radius:9px;padding:9px}}
    .pipe b{{display:block;font-size:18px}} .pipe small{{color:#8e99ad!important}} .continuity{{margin-top:12px;border-radius:10px;padding:10px 12px;font-size:13px}}
    .continuity.warm{{background:#392f13;color:#fde68a}} .continuity.bad{{background:#431b22;color:#fda4af}} .continuity.idle{{background:#222735;color:#b7c0d2}}
    .continuity.ok{{background:#123c2b;color:#79f2b2}}
    .stem-note{{margin-top:10px;color:#8e99ad!important;font-size:12px}}
    @media(max-width:720px){{.stem-grid{{grid-template-columns:1fr}}.stem-visuals{{grid-template-columns:1fr}}.stem-orb-panel{{min-height:158px}}}}
    @media (prefers-reduced-motion:reduce){{.stem-orb span,.stem-wave i{{animation:none!important}}.meter b{{transition:none}}}}
  </style>
  <div class="stem-head"><strong>实时输入电平</strong><span class="stem-chip {state_class}">{state_text} · {html.escape(snapshot['codec'])}</span></div>
  <div class="stem-track"><div><b>{title}</b><small>{track_details or '内容指纹将在收到音频后确认'}</small></div><time>{progress}</time></div>
  <div class="stem-visuals">
    <div class="stem-orb-panel">
      <div class="stem-orb" role="img" aria-label="音频能量可视球，当前强度 {energy_percent}%" style="{orb_style}">
        <span class="stem-orb-halo"></span><span class="stem-orb-ring"></span><span class="stem-orb-ring secondary"></span><span class="stem-orb-core"></span><span class="stem-orb-sheen"></span>
      </div>
      <small class="stem-orb-caption">AUDIO ENERGY · {energy_percent}%</small>
    </div>
    <div class="stem-wave-panel"><div class="stem-wave-title"><span>平滑实时波形</span><span>浏览器合成动画</span></div><div class="stem-wave">{bars}</div></div>
  </div>
  <div class="stem-lyrics"><h4>同步歌词 · {lyrics_source}</h4>{lyrics_body}</div>
  <div class="stem-grid">
    <div class="stem-card">
      <h4>立体声电平</h4>
      <div class="meter">L {meter(snapshot['peak_left'])}</div>
      <div class="meter">R {meter(snapshot['peak_right'])}</div>
      <div class="stem-note">已接收 {snapshot['received_seconds']:.1f} 秒 PCM · {"Windows WASAPI 系统捕获" if snapshot['input_source'] == 'windows' else 'AirPlay 自身默认延迟约 0.25 秒'}</div>
    </div>
    <div class="stem-card">
      <h4>{snapshot['analysis_window_seconds']:g} 秒分析窗 / {snapshot['hop_seconds']:g} 秒步进</h4>
      <div class="pipe">
        <div><b>{snapshot['captured_sequence']}</b><small>捕获窗口</small></div>
        <div><b>{snapshot['gpu_sequence']}</b><small>处理完成</small></div>
        <div><b>{snapshot['playback_sequence']}</b><small>正在监听</small></div>
      </div>
      <div class="stem-note">待处理 {snapshot['pending_windows']} 窗 · 可播放缓存 {snapshot['ready_buffer_seconds']:g} 秒 · 预缓冲 {snapshot['prebuffer_seconds']:g} 秒 · 单窗耗时 {processing_text}</div>
      <div class="stem-note">设备打开 {snapshot['device_open_count']} 次 · 自动恢复 {snapshot['device_recoveries']} 次 · 分轨跳窗至 {snapshot['skipped_sequence']} · 设备缓冲 {snapshot['device_buffer_ms']:g} ms · 累计欠载 {snapshot['underruns']} 次{underrun_event_text}</div>
      <div class="stem-note">{device_network_text}</div>
      <div class="stem-note">混音控制 {snapshot['mixer_updates']} 次 · 最近 {snapshot['last_mixer_control_latency_ms']:g} ms · 最慢 {snapshot['max_mixer_control_latency_ms']:g} ms · {snapshot['gain_smoothing_ms']:g} ms 平滑</div>
      <div class="stem-note">相邻结果按同一时间轴交叠 {snapshot['overlap_milliseconds']:g} ms 后平滑拼接</div>
      <div class="stem-note">{gain_text}</div>
      <div class="stem-note">{cache_text}</div>
      <div class="stem-note">{model_text}</div>
    </div>
  </div>
  <div class="continuity {continuity_class}">{continuity}</div>
  <div class="continuity {acceptance_class}"><b>真机自动验收：{acceptance_labels[snapshot['acceptance_state']]}</b><br>{acceptance_checklist}</div>
  <div class="stem-note">首次播放按窗口边收、边分离、边写入内容寻址缓存；相同 PCM、模型和轨数再次出现时直接读取本地多轨结果。缓存按最近使用顺序自动限额清理。</div>
</div>"""


def monitor_choices(profile_name: str) -> list[tuple[str, str]]:
    if profile_name not in LIVE_PROFILES:
        raise ValueError("未知实时分离模式。")
    return [(STEM_LABELS[stem], stem) for stem in LIVE_PROFILES[profile_name].stems]


def all_monitor_choices() -> list[tuple[str, str]]:
    return [(label, stem) for stem, label in STEM_LABELS.items()]


def active_stem_visibility(profile_name: str) -> dict[str, bool]:
    if profile_name not in LIVE_PROFILES:
        raise ValueError("未知实时分离模式。")
    active_stems = set(LIVE_PROFILES[profile_name].stems)
    return {stem: stem in active_stems for stem in STEM_LABELS}


def write_mixer_control(
    live_root: str | Path,
    profile_name: str,
    gains: Mapping[str, float],
) -> int:
    if profile_name not in LIVE_PROFILES:
        raise ValueError("未知实时分离模式。")
    profile = LIVE_PROFILES[profile_name]
    missing = [stem for stem in profile.stems if stem not in gains]
    if missing:
        raise ValueError(f"混音快照缺少音轨：{', '.join(missing)}")

    normalized: dict[str, float] = {}
    for stem in profile.stems:
        try:
            gain = float(gains[stem])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须是数字。") from exc
        if not math.isfinite(gain):
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须是有限数字。")
        if not 0.0 <= gain <= 1.0:
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须在 0 到 1 之间。")
        normalized[stem] = gain

    root = Path(live_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"mixer-control-{len(profile.stems)}.tsv"
    partial = destination.with_suffix(".tsv.part")
    with _MIXER_WRITE_LOCK:
        previous_sequence = 0
        try:
            existing_lines = destination.read_text(encoding="utf-8-sig").splitlines()
            if len(existing_lines) >= 2 and existing_lines[1].startswith("sequence\t"):
                previous_sequence = max(0, int(existing_lines[1].split("\t", 1)[1]))
        except (OSError, ValueError):
            pass
        sequence = max(time.time_ns(), previous_sequence + 1)
        lines = ["stem-studio-mixer-v1", f"sequence\t{sequence}"]
        lines.extend(f"{stem}\t{normalized[stem]:.6f}" for stem in profile.stems)
        partial.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _replace_with_sharing_retry(partial, destination)
        return sequence


def write_mixer_percentages(
    live_root: str | Path,
    profile_name: str,
    percentages: Mapping[str, float],
) -> int:
    if profile_name not in LIVE_PROFILES:
        raise ValueError("未知实时分离模式。")
    normalized: dict[str, float] = {}
    for stem in LIVE_PROFILES[profile_name].stems:
        if stem not in percentages:
            raise ValueError(f"混音快照缺少音轨：{stem}")
        try:
            percentage = float(percentages[stem])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须是数字。") from exc
        if not math.isfinite(percentage):
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须是有限数字。")
        if not 0.0 <= percentage <= 100.0:
            raise ValueError(f"{STEM_LABELS[stem]} 音量必须在 0 到 100 之间。")
        normalized[stem] = percentage / 100.0
    return write_mixer_control(live_root, profile_name, normalized)


def read_processes(live_root: str | Path) -> list[tuple[str, int]]:
    system_choice = [("全部 Windows 音频（推荐）", 0)]
    path = Path(live_root) / "processes.json"
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))
        candidates = []
        for item in entries:
            pid = int(item["pid"])
            name = str(item["name"]).strip()
            raw_title = item.get("title")
            title = "" if raw_title is None else str(raw_title).strip()
            if pid > 0 and name:
                candidates.append((name, title, pid))
        grouped: dict[str, tuple[str, str, int]] = {}
        for candidate in candidates:
            name, title, pid = candidate
            key = name.casefold()
            current = grouped.get(key)
            if current is None or (bool(title), -pid) > (bool(current[1]), -current[2]):
                grouped[key] = candidate
        music_names = ("spotify", "cloudmusic", "qqmusic", "music", "foobar", "aimp", "vlc", "potplayer")
        ordered = sorted(
            grouped.values(),
            key=lambda item: (
                0 if any(token in item[0].casefold() for token in music_names) else 1 if item[1] else 2,
                item[0].casefold(),
            ),
        )
        return system_choice + [
            (f"{title or name} · {name} · PID {pid}", pid)
            for name, title, pid in ordered
        ]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return system_choice


def write_command(
    live_root: str | Path,
    action: str,
    process_id: int | None = None,
    monitor_stem: str = "instrumental",
    profile_name: str = "人声 / 伴奏 · 高质量",
    device_endpoint: str = "",
    hop_seconds: int | None = None,
) -> int:
    if action not in {"start", "start_airplay", "stop", "open_audio_settings"}:
        raise ValueError("未知实时控制命令。")
    if action == "start" and (process_id is None or int(process_id) < 0):
        raise ValueError("请先选择有效的 Windows 音频来源。")
    if action in {"start", "start_airplay"}:
        if hop_seconds is not None and hop_seconds not in {3, 6}:
            raise ValueError("实时步进仅支持 3 秒或 6 秒。")
        if profile_name not in LIVE_PROFILES:
            raise ValueError("未知实时分离模式。")
        profile = LIVE_PROFILES[profile_name]
        if monitor_stem not in profile.stems:
            raise ValueError(f"{profile_name} 不产生 {monitor_stem} 音轨。")
    root = Path(live_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {"action": action}
    if process_id is not None:
        payload["process_id"] = int(process_id)
    if action in {"start", "start_airplay"}:
        payload["monitor_stem"] = monitor_stem
        payload["profile_name"] = profile_name
        payload["track_count"] = len(profile.stems)
        if hop_seconds is not None:
            payload["hop_seconds"] = hop_seconds
        normalized_device_endpoint = _normalize_device_endpoint(device_endpoint)
        if normalized_device_endpoint:
            payload["device_endpoint"] = normalized_device_endpoint
        if action == "start_airplay":
            payload["input_source"] = "airplay"
    destination = root / "command.json"
    partial = destination.with_suffix(".json.part")
    with _COMMAND_WRITE_LOCK:
        previous_sequence = 0
        try:
            previous_sequence = max(
                0,
                int(_read_status(destination).get("sequence", 0)),
            )
        except (TypeError, ValueError):
            pass
        sequence = max(time.time_ns(), previous_sequence + 1)
        payload = {"sequence": sequence, **payload}
        partial.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, destination)
        return sequence


def routing_markdown(live_root: str | Path) -> str:
    path = Path(live_root) / "audio-routing.json"
    if not path.is_file():
        return "⚪ **正在检测纯净监听条件** · 等待 Windows 音频端点清单"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return "🟡 **音频路由状态暂不可读** · 可稍后刷新"
    if payload.get("virtual_device_found"):
        devices = ", ".join(str(item) for item in payload.get("virtual_devices", []))
        return (
            f"🟢 **纯净监听已具备条件** · 检测到 {devices}。"
            "请在 Windows 音量混合器中把音乐软件输出切到该虚拟设备，分离结果仍播放到默认扬声器。"
        )
    error = payload.get("error")
    if error:
        return f"🟡 **无法检测虚拟音频设备** · {error}"
    return (
        "🟡 **未检测到虚拟音频设备** · 当前可捕获并监听分离结果，但原声也会同时播放。"
        "若只想听分离音轨，请先安装 VB-CABLE、VoiceMeeter 或其他可信虚拟音频设备。"
    )


def status_markdown(live_root: str | Path) -> str:
    root = Path(live_root)
    pipeline = live_pipeline_snapshot(root)
    if pipeline["controller_stalled"]:
        age = pipeline["controller_heartbeat_age_seconds"]
        age_text = f"{age:.1f} 秒" if age is not None else "未知时长"
        return (
            f"🔴 **控制器心跳已停止 {age_text}** · 当前实时状态可能已过期，"
            "请重新启动 Stem Studio"
        )
    statuses = []
    for filename in (
        "controller-status.json",
        "airplay-status.json",
        "gpu-status.json",
        "playback-status.json",
    ):
        path = root / filename
        if path.is_file():
            try:
                statuses.append(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, json.JSONDecodeError):
                pass
    error = next((item.get("error") for item in statuses if item.get("error")), None)
    if error:
        recovering = next((item for item in statuses if item.get("recovering")), None)
        if recovering:
            return (
                f"🟡 **已隔离失败窗口 {recovering.get('failed_sequence', '')}，正在继续** · "
                f"{error}"
            )
        return f"🔴 **实时链路错误** · {error}"
    capture = next((item for item in statuses if "process_id" in item), {})
    airplay = next((item for item in statuses if "receiver" in item), {})
    gpu = next((item for item in statuses if "last_sequence" in item), {})
    playback = next((item for item in statuses if "stem" in item), {})
    if airplay.get("enabled"):
        if pipeline["stream_stalled"]:
            return (
                "🟠 **AirPlay PCM 已停止更新** · 手机可能已暂停或断开，"
                "请确认后重新选择 Stem Studio"
            )
        if pipeline["streaming"]:
            return (
                f"🟢 **AirPlay PCM 正在接收** · {airplay.get('codec', '未知编码')} → "
                f"PCM16 · 已发布窗口 {airplay.get('published_windows', 0)}"
            )
        return "🟡 **等待手机 AirPlay** · 在控制中心选择 Stem Studio"
    if capture.get("state") == "capturing":
        profile_name = gpu.get("profile_name") or capture.get("profile_name") or "实时分离"
        capture_label = (
            "全部 Windows 音频"
            if capture.get("input_scope") == "system"
            else f"PID {capture['process_id']}"
        )
        return (
            f"🟢 **正在捕获 {capture_label}** · "
            f"{profile_name} · "
            f"GPU 已完成窗口 {gpu.get('last_sequence', 0)} · "
            f"监听 {playback.get('state', '等待')} {playback.get('sequence', '')} · "
            "12 秒分析窗 / 6 秒步进连续输出"
        )
    return "⚪ **实时链路待机** · 选择正在播放音乐的软件后启动"
