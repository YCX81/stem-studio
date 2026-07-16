from __future__ import annotations

import json
import html
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .core import LIVE_PROFILES


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
    profile_name = "人声 / 伴奏 · 高质量"

    if state == "airplay_waiting" and controller_online:
        input_source = "airplay"
        candidate = controller.get("profile_name")
        if candidate in LIVE_PROFILES:
            profile_name = str(candidate)
    elif state == "capturing" and controller_online:
        candidate = controller.get("profile_name")
        if candidate in LIVE_PROFILES:
            profile_name = str(candidate)
    elif not state:
        command = _read_status(root / "command.json")
        if command.get("action") in {"start", "start_airplay"}:
            input_source = (
                "airplay" if command.get("action") == "start_airplay" else "process"
            )
            candidate = command.get("profile_name")
            if candidate in LIVE_PROFILES:
                profile_name = str(candidate)

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
        "profile_name": profile_name,
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
    raw_streaming = airplay.get("state") == "streaming"
    streaming = raw_streaming and (
        airplay_status_age_seconds is not None
        and airplay_status_age_seconds <= _AIRPLAY_STREAM_STALE_SECONDS
    )
    stream_stalled = raw_streaming and not streaming
    gpu = _read_status(root / "gpu-status.json")
    playback = _read_status(root / "playback-status.json")
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
            return min(1.0, max(0.0, float(airplay.get(name, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    waveform = []
    for value in airplay.get("waveform", [])[:64]:
        try:
            waveform.append(min(1.0, max(0.0, float(value))))
        except (TypeError, ValueError):
            waveform.append(0.0)

    raw_track = airplay.get("track", {})
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
        "airplay_status_age_seconds": (
            round(airplay_status_age_seconds, 3)
            if airplay_status_age_seconds is not None
            else None
        ),
        "codec": str(airplay.get("codec", "none")),
        "received_seconds": round(max(0, int(airplay.get("pcm_frames", 0) or 0)) / 44_100, 1),
        "published_windows": max(0, int(airplay.get("published_windows", 0) or 0)),
        "peak_left": level("peak_left"),
        "peak_right": level("peak_right"),
        "rms_left": level("rms_left"),
        "rms_right": level("rms_right"),
        "waveform": waveform,
        "track_revision": max(0, int(track_number("revision"))),
        "track_title": track_text("title"),
        "track_artist": track_text("artist"),
        "track_album": track_text("album"),
        "track_position_seconds": round(track_number("position_seconds"), 3),
        "track_duration_seconds": round(track_number("duration_seconds"), 3),
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
    bars = "".join(
        f'<i style="height:{max(4, round(value * 100))}%"></i>' for value in waveform
    )
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
        else "正在接收音频"
        if streaming
        else "上游 PCM 已停止"
        if stream_stalled
        else "等待 AirPlay 音频"
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
    model_text = (
        f"{model_labels[snapshot['model_state']]} · {process_text}"
        f" · 硬截止 {snapshot['inference_timeout_seconds']:g} 秒"
        f" · {warmup_text}"
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

    title = html.escape(snapshot["track_title"] or "等待手机发送曲目信息")
    artist = html.escape(snapshot["track_artist"])
    album = html.escape(snapshot["track_album"])
    track_details = " · ".join(value for value in (artist, album) if value)
    duration = snapshot["track_duration_seconds"]
    progress = (
        f"{clock_text(snapshot['track_position_seconds'])} / {clock_text(duration)}"
        if duration > 0.0
        else "进度待同步"
    )
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
    .stem-wave{{height:92px;display:flex;align-items:center;gap:3px;background:#0a0c11;border-radius:10px;padding:10px;overflow:hidden}}
    .stem-wave i{{display:block;flex:1;min-width:2px;max-height:100%;border-radius:3px;background:linear-gradient(180deg,#67e8f9,#8b5cf6);opacity:.9}}
    .stem-grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-top:14px}} .stem-card{{background:#171b24;border-radius:12px;padding:13px}}
    .stem-card h4{{font-size:12px;color:#aab3c5!important;margin:0 0 10px}} .meter{{color:#eef2ff!important;display:grid;grid-template-columns:14px 1fr 38px;gap:8px;align-items:center;margin:7px 0;font-size:12px}}
    .meter span{{height:8px;background:#2b3140;border-radius:8px;overflow:hidden}} .meter b{{display:block;height:100%;background:#22d3ee;border-radius:8px}} .meter em{{font-style:normal;color:#aab3c5!important;text-align:right}}
    .pipe{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .pipe div{{background:#0e1118;border-radius:9px;padding:9px}}
    .pipe b{{display:block;font-size:18px}} .pipe small{{color:#8e99ad!important}} .continuity{{margin-top:12px;border-radius:10px;padding:10px 12px;font-size:13px}}
    .continuity.warm{{background:#392f13;color:#fde68a}} .continuity.bad{{background:#431b22;color:#fda4af}} .continuity.idle{{background:#222735;color:#b7c0d2}}
    .continuity.ok{{background:#123c2b;color:#79f2b2}}
    .stem-note{{margin-top:10px;color:#8e99ad!important;font-size:12px}} @media(max-width:720px){{.stem-grid{{grid-template-columns:1fr}}}}
  </style>
  <div class="stem-head"><strong>实时输入电平</strong><span class="stem-chip {state_class}">{state_text} · {html.escape(snapshot['codec'])}</span></div>
  <div class="stem-track"><div><b>{title}</b><small>{track_details or '内容指纹将在收到音频后确认'}</small></div><time>{progress}</time></div>
  <div class="stem-wave">{bars}</div>
  <div class="stem-grid">
    <div class="stem-card">
      <h4>立体声电平</h4>
      <div class="meter">L {meter(snapshot['peak_left'])}</div>
      <div class="meter">R {meter(snapshot['peak_right'])}</div>
      <div class="stem-note">已接收 {snapshot['received_seconds']:.1f} 秒 PCM · AirPlay 自身默认延迟约 0.25 秒</div>
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
        return [(f"{title or name} · {name} · PID {pid}", pid) for name, title, pid in ordered]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return []


def write_command(
    live_root: str | Path,
    action: str,
    process_id: int | None = None,
    monitor_stem: str = "instrumental",
    profile_name: str = "人声 / 伴奏 · 高质量",
) -> int:
    if action not in {"start", "start_airplay", "stop", "open_audio_settings"}:
        raise ValueError("未知实时控制命令。")
    if action == "start" and (process_id is None or int(process_id) <= 0):
        raise ValueError("请先选择有效的音乐软件。")
    if action in {"start", "start_airplay"}:
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
        return (
            f"🟢 **正在捕获 PID {capture['process_id']}** · "
            f"{profile_name} · "
            f"GPU 已完成窗口 {gpu.get('last_sequence', 0)} · "
            f"监听 {playback.get('state', '等待')} {playback.get('sequence', '')} · "
            "12 秒分析窗 / 6 秒步进连续输出"
        )
    return "⚪ **实时链路待机** · 选择正在播放音乐的软件后启动"
