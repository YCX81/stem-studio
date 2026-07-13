import json
from pathlib import Path

import pytest

from stemstudio.live_control import (
    all_monitor_choices,
    monitor_choices,
    read_processes,
    routing_markdown,
    status_markdown,
    write_command,
)


def test_monitor_choices_follow_selected_live_profile() -> None:
    assert monitor_choices("人声 / 伴奏 · 高质量") == [
        ("人声", "vocals"),
        ("伴奏（去人声）", "instrumental"),
    ]
    assert monitor_choices("六轨 · 加吉他/钢琴") == [
        ("人声", "vocals"),
        ("鼓", "drums"),
        ("贝斯", "bass"),
        ("吉他", "guitar"),
        ("钢琴", "piano"),
        ("其他", "other"),
    ]
    assert {value for _label, value in all_monitor_choices()} == {
        "vocals",
        "instrumental",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
    }


def test_read_processes_builds_stable_pid_choices(tmp_path: Path) -> None:
    (tmp_path / "processes.json").write_text(
        json.dumps(
            [
                {"pid": 44, "name": "Music.exe", "title": None},
                {"pid": 42, "name": "Music.exe", "title": "正在播放"},
                {"pid": 9, "name": "Utility.exe", "title": None},
            ]
        ),
        encoding="utf-8",
    )
    assert read_processes(tmp_path) == [
        ("正在播放 · Music.exe · PID 42", 42),
        ("Utility.exe · Utility.exe · PID 9", 9),
    ]


def test_write_command_is_atomic_and_validates_pid(tmp_path: Path) -> None:
    sequence = write_command(
        tmp_path,
        "start",
        42,
        monitor_stem="guitar",
        profile_name="六轨 · 加吉他/钢琴",
    )
    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload == {
        "sequence": sequence,
        "action": "start",
        "process_id": 42,
        "monitor_stem": "guitar",
        "profile_name": "六轨 · 加吉他/钢琴",
    }
    assert not (tmp_path / "command.json.part").exists()
    with pytest.raises(ValueError, match="音乐软件"):
        write_command(tmp_path, "start", 0)


def test_write_command_rejects_stem_not_produced_by_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不产生"):
        write_command(
            tmp_path,
            "start",
            42,
            monitor_stem="guitar",
            profile_name="四轨 · 人声/鼓/贝斯/其他",
        )


def test_write_command_can_open_windows_audio_routing_without_pid(tmp_path: Path) -> None:
    sequence = write_command(tmp_path, "open_audio_settings")
    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload == {"sequence": sequence, "action": "open_audio_settings"}


def test_routing_markdown_reports_virtual_device_readiness(tmp_path: Path) -> None:
    (tmp_path / "audio-routing.json").write_text(
        json.dumps(
            {
                "virtual_device_found": True,
                "virtual_devices": ["CABLE Input (VB-Audio Virtual Cable)"],
            }
        ),
        encoding="utf-8",
    )
    ready = routing_markdown(tmp_path)
    assert "纯净监听已具备条件" in ready
    assert "CABLE Input" in ready

    (tmp_path / "audio-routing.json").write_text(
        json.dumps({"virtual_device_found": False, "virtual_devices": []}),
        encoding="utf-8",
    )
    missing = routing_markdown(tmp_path)
    assert "未检测到虚拟音频设备" in missing


def test_status_markdown_combines_capture_and_gpu_state(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "capturing", "process_id": 42}), encoding="utf-8"
    )
    (tmp_path / "gpu-status.json").write_text(
        json.dumps({"state": "running", "last_sequence": 3}), encoding="utf-8"
    )
    status = status_markdown(tmp_path)
    assert "PID 42" in status
    assert "窗口 3" in status
