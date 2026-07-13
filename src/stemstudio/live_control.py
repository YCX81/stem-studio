from __future__ import annotations

import json
import os
import time
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


def monitor_choices(profile_name: str) -> list[tuple[str, str]]:
    if profile_name not in LIVE_PROFILES:
        raise ValueError("未知实时分离模式。")
    return [(STEM_LABELS[stem], stem) for stem in LIVE_PROFILES[profile_name].stems]


def all_monitor_choices() -> list[tuple[str, str]]:
    return [(label, stem) for stem, label in STEM_LABELS.items()]


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
    if action not in {"start", "stop", "open_audio_settings"}:
        raise ValueError("未知实时控制命令。")
    if action == "start" and (process_id is None or int(process_id) <= 0):
        raise ValueError("请先选择有效的音乐软件。")
    if action == "start":
        if profile_name not in LIVE_PROFILES:
            raise ValueError("未知实时分离模式。")
        profile = LIVE_PROFILES[profile_name]
        if monitor_stem not in profile.stems:
            raise ValueError(f"{profile_name} 不产生 {monitor_stem} 音轨。")
    root = Path(live_root)
    root.mkdir(parents=True, exist_ok=True)
    sequence = time.time_ns()
    payload = {"sequence": sequence, "action": action}
    if process_id is not None:
        payload["process_id"] = int(process_id)
    if action == "start":
        payload["monitor_stem"] = monitor_stem
        payload["profile_name"] = profile_name
    destination = root / "command.json"
    partial = destination.with_suffix(".json.part")
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
    statuses = []
    for filename in ("controller-status.json", "gpu-status.json", "playback-status.json"):
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
    gpu = next((item for item in statuses if "last_sequence" in item), {})
    playback = next((item for item in statuses if "stem" in item), {})
    if capture.get("state") == "capturing":
        profile_name = gpu.get("profile_name") or capture.get("profile_name") or "实时分离"
        return (
            f"🟢 **正在捕获 PID {capture['process_id']}** · "
            f"{profile_name} · "
            f"GPU 已完成窗口 {gpu.get('last_sequence', 0)} · "
            f"监听 {playback.get('state', '等待')} {playback.get('sequence', '')} · "
            "12 秒窗口流式输出"
        )
    return "⚪ **实时链路待机** · 选择正在播放音乐的软件后启动"
