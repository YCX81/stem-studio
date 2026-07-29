import json
import os
import time
from pathlib import Path

import pytest
import stemstudio.live_control as live_control_module

from stemstudio.live_control import (
    all_monitor_choices,
    active_stem_visibility,
    live_dashboard_html,
    live_pipeline_snapshot,
    live_ui_defaults,
    monitor_choices,
    read_processes,
    routing_markdown,
    status_markdown,
    write_command,
    write_mixer_control,
)


def test_live_pipeline_snapshot_exposes_levels_buffer_and_processing_headroom(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "outbox").mkdir()
    for sequence in (1, 2, 3):
        (tmp_path / "inbox" / f"capture-{sequence:08d}.wav").touch()
    for sequence, processing in ((1, 2.5), (2, 2.7)):
        (tmp_path / "outbox" / f"result-{sequence:08d}.json").write_text(
            json.dumps({"sequence": sequence, "processing_seconds": processing}),
            encoding="utf-8",
        )
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "enabled": True,
                "codec": "ALAC",
                "pcm_frames": 529_200,
                "published_windows": 3,
                "peak_left": 0.75,
                "peak_right": 0.5,
                "rms_left": 0.3,
                "rms_right": 0.2,
                "waveform": [0.1, 0.4, 0.8, 0.2],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "gpu-status.json").write_text(
        json.dumps({"state": "running", "last_sequence": 2}), encoding="utf-8"
    )
    (tmp_path / "playback-status.json").write_text(
        json.dumps({"state": "playing", "sequence": 1, "stem": "vocals"}), encoding="utf-8"
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["streaming"] is True
    assert snapshot["received_seconds"] == 12.0
    assert snapshot["peak_left"] == 0.75
    assert snapshot["captured_sequence"] == 3
    assert snapshot["gpu_sequence"] == 2
    assert snapshot["pending_windows"] == 1
    assert snapshot["ready_buffer_seconds"] == 12
    assert snapshot["processing_seconds"] == 2.7
    assert snapshot["sustainable"] is True
    dashboard = live_dashboard_html(tmp_path)
    assert "实时输入电平" in dashboard
    assert "12 秒" in dashboard
    assert "2.70 秒 / 6 秒" in dashboard
    assert "50 ms" in dashboard
    assert "持久 WASAPI 输出队列" in dashboard


def test_windows_capture_status_drives_levels_and_media_metadata(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "process_id": 0,
                "input_scope": "system",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    (tmp_path / "capture-input-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "source": "windows",
                "input_scope": "system",
                "pcm_frames": 88_200,
                "signal_detected": True,
                "source_sessions_isolated": 3,
                "source_isolation_db": -80,
                "peak_left": 0.8,
                "peak_right": 0.6,
                "rms_left": 0.4,
                "rms_right": 0.3,
                "waveform": [0.2, 0.5, 0.9],
                "track": {
                    "revision": 9,
                    "title": "Windows Song",
                    "artist": "Desktop Artist",
                    "album": "Desktop Album",
                    "position_seconds": 12.5,
                    "duration_seconds": 180,
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["input_source"] == "windows"
    assert snapshot["input_scope"] == "system"
    assert snapshot["streaming"] is True
    assert snapshot["signal_detected"] is True
    assert snapshot["source_sessions_isolated"] == 3
    assert snapshot["source_isolation_db"] == -80
    assert snapshot["received_seconds"] == 2.0
    assert snapshot["peak_left"] == 0.8
    assert snapshot["track_title"] == "Windows Song"
    dashboard = live_dashboard_html(tmp_path)
    assert "已捕获 Windows 音频" in dashboard
    assert "Windows Song" in dashboard
    assert "Desktop Artist" in dashboard
    assert "Windows WASAPI 系统捕获" in dashboard


def test_windows_playing_without_process_session_is_not_labeled_as_paused(
    tmp_path: Path,
) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "capturing", "process_id": 42}),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    (tmp_path / "capture-input-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "source": "windows",
                "input_scope": "process",
                "pcm_frames": 44_100,
                "signal_detected": False,
                "source_sessions_tracked": 0,
                "track": {"title": "Protected Song", "playing": True},
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)
    dashboard = live_dashboard_html(tmp_path)

    assert snapshot["track_playing"] is True
    assert snapshot["source_sessions_tracked"] == 0
    assert "播放器正在播放，但按进程未捕获到音频" in dashboard


def test_stale_airplay_stream_is_reported_as_stalled_not_active(
    tmp_path: Path,
) -> None:
    airplay_path = tmp_path / "airplay-status.json"
    airplay_path.write_text(
        json.dumps(
            {
                "state": "streaming",
                "enabled": True,
                "receiver": "Stem Studio",
                "codec": "ALAC",
                "pcm_frames": 529_200,
                "published_windows": 2,
            }
        ),
        encoding="utf-8",
    )
    stale_time = time.time() - 10.0
    os.utime(airplay_path, (stale_time, stale_time))

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["streaming"] is False
    assert snapshot["stream_stalled"] is True
    assert snapshot["airplay_status_age_seconds"] >= 9.0
    assert "上游 PCM 已停止" in live_dashboard_html(tmp_path)
    assert "AirPlay PCM 已停止更新" in status_markdown(tmp_path)


def test_live_dashboard_forces_readable_text_on_dark_theme(tmp_path: Path) -> None:
    dashboard = live_dashboard_html(tmp_path)

    assert ".stem-live,.stem-live strong,.stem-live b" in dashboard
    assert "color:#eef2ff!important" in dashboard
    assert ".stem-track small{color:#8e99ad!important}" in dashboard
    assert ".stem-track time{white-space:nowrap;color:#a5b4fc!important" in dashboard
    assert ".stem-card h4{font-size:12px;color:#aab3c5!important" in dashboard
    assert ".meter{color:#eef2ff!important;" in dashboard
    assert ".meter em{font-style:normal;color:#aab3c5!important" in dashboard
    assert ".pipe small{color:#8e99ad!important}" in dashboard
    assert ".stem-note{margin-top:10px;color:#8e99ad!important" in dashboard


def test_live_dashboard_exposes_model_readiness_deadline_and_fallback_counters(
    tmp_path: Path,
) -> None:
    (tmp_path / "gpu-status.json").write_text(
        json.dumps(
            {
                "state": "waiting",
                "model_state": "ready",
                "inference_process_pid": 4321,
                "inference_timeout_seconds": 5.5,
                "model_warmup_seconds": 8.25,
                "warmup_windows": 1,
                "deadline_windows": 2,
                "low_buffer_fallback_windows": 3,
                "continuity_reserve_seconds": 7.0,
                "max_processing_seconds": 4.2,
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["model_state"] == "ready"
    assert snapshot["inference_process_pid"] == 4321
    assert snapshot["inference_timeout_seconds"] == 5.5
    assert snapshot["model_warmup_seconds"] == 8.25
    assert snapshot["warmup_windows"] == 1
    assert snapshot["deadline_windows"] == 2
    assert snapshot["low_buffer_fallback_windows"] == 3
    assert snapshot["continuity_reserve_seconds"] == 7.0
    assert snapshot["max_processing_seconds"] == 4.2
    dashboard = live_dashboard_html(tmp_path)
    assert "模型就绪" in dashboard
    assert "PID 4321" in dashboard
    assert "硬截止 5.5 秒" in dashboard
    assert "完整预热 8.25 秒" in dashboard
    assert "预热保底 1 窗" in dashboard
    assert "超时保底 2 窗" in dashboard
    assert "低余量保底 3 窗" in dashboard
    assert "连续性安全线 7 秒" in dashboard
    assert "最慢 4.20 秒" in dashboard


def test_live_pipeline_snapshot_prefers_native_queue_and_mixer_metrics(tmp_path: Path) -> None:
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "version": 2,
                "state": "playing",
                "sequence": 9,
                "skipped_sequence": 8,
                "buffered_seconds": 18.4,
                "prebuffer_seconds": 12.0,
                "underruns": 2,
                "last_underrun_system_time_ns": 1_700_000_000_000_000_000,
                "last_underrun_buffered_frames": 441,
                "last_underrun_total_read_frames": 88_200,
                "device_open_count": 4,
                "device_recoveries": 3,
                "device_recovering": True,
                "last_device_hresult": "0x88890004",
                "device_buffer_frames": 1036,
                "analysis_window_seconds": 12.0,
                "hop_seconds": 6.0,
                "overlap_milliseconds": 100.0,
                "control_sequence": 77,
                "mixer_updates": 21,
                "last_mixer_control_latency_ms": 18.4,
                "max_mixer_control_latency_ms": 37.2,
                "gain_smoothing_ms": 20.0,
                "gains": {"vocals": 0.75, "instrumental": 0.0},
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["ready_buffer_seconds"] == 18.4
    assert snapshot["skipped_sequence"] == 8
    assert snapshot["prebuffer_seconds"] == 12.0
    assert snapshot["underruns"] == 2
    assert snapshot["last_underrun_system_time_ns"] == 1_700_000_000_000_000_000
    assert snapshot["last_underrun_buffer_seconds"] == 0.01
    assert snapshot["last_underrun_playback_seconds"] == 2.0
    assert snapshot["device_open_count"] == 4
    assert snapshot["device_recoveries"] == 3
    assert snapshot["device_recovering"] is True
    assert snapshot["last_device_hresult"] == "0x88890004"
    assert snapshot["device_buffer_ms"] == pytest.approx(23.5, abs=0.1)
    assert snapshot["control_sequence"] == 77
    assert snapshot["mixer_updates"] == 21
    assert snapshot["last_mixer_control_latency_ms"] == 18.4
    assert snapshot["max_mixer_control_latency_ms"] == 37.2
    assert snapshot["gain_smoothing_ms"] == 20.0
    assert snapshot["analysis_window_seconds"] == 12.0
    assert snapshot["hop_seconds"] == 6.0
    assert snapshot["overlap_milliseconds"] == 100.0
    assert snapshot["gains"] == {"vocals": 0.75, "instrumental": 0.0}
    dashboard = live_dashboard_html(tmp_path)
    assert "输出设备正在自动重连" in dashboard
    assert "0x88890004" in dashboard
    assert "设备打开 4 次" in dashboard
    assert "自动恢复 3 次" in dashboard
    assert "欠载 2 次" in dashboard
    assert "上次欠载" in dashboard
    assert "当时队列 0.01 秒" in dashboard
    assert "播放时间轴 2 秒" in dashboard
    assert "混音控制 21 次" in dashboard
    assert "最近 18.4 ms" in dashboard
    assert "最慢 37.2 ms" in dashboard
    assert "20 ms 平滑" in dashboard
    assert "人声 75%" in dashboard
    assert "伴奏（去人声） 0%" in dashboard


def test_live_pipeline_snapshot_retries_transient_atomic_status_read_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    playback_path = tmp_path / "playback-status.json"
    playback_path.write_text(
        json.dumps({"state": "playing", "sequence": 12, "underruns": 0}),
        encoding="utf-8",
    )
    real_read_text = Path.read_text
    attempts = 0
    waits: list[float] = []

    def flaky_read_text(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        if path == playback_path:
            attempts += 1
            if attempts < 3:
                raise PermissionError("simulated atomic replacement conflict")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    monkeypatch.setattr(live_control_module.time, "sleep", waits.append)

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["playback_sequence"] == 12
    assert snapshot["playback_state"] == "playing"
    assert attempts == 3
    assert waits[:2] == [0.002, 0.002]


def test_live_dashboard_distinguishes_idle_drain_from_active_underrun(
    tmp_path: Path,
) -> None:
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "codec": "ALAC",
                "pcm_frames": 44_100,
                "sample_rate": 44_100,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "state": "playing",
                "sequence": 12,
                "underruns": 1,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "acceptance-report.json").write_text(
        json.dumps(
            {
                "state": "in_progress",
                "requirements": {"zero_active_underruns": True},
                "metrics": {
                    "active_samples": 10,
                    "active_underrun_delta": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)
    dashboard = live_dashboard_html(tmp_path)

    assert snapshot["active_underrun_delta"] == 0
    assert snapshot["acceptance_active_samples"] == 10
    assert "本次连续播放流内欠载 0 次" in dashboard
    assert "累计 1 次来自暂停或断流阶段" in dashboard
    assert "输出队列已发生 1 次欠载" not in dashboard


def test_live_dashboard_makes_content_cache_hit_explicit(tmp_path: Path) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "result-00000012.json").write_text(
        json.dumps(
            {
                "sequence": 12,
                "processing_seconds": 0.0,
                "cache_hit": True,
                "cache_scope": "song",
                "cache_key": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "gpu-status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "last_sequence": 12,
                "cache_hits": 5,
                "cache_misses": 8,
                "songs_cached": 3,
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["cache_hit"] is True
    assert snapshot["cache_hits"] == 5
    assert snapshot["cache_misses"] == 8
    assert snapshot["cache_scope"] == "song"
    assert snapshot["songs_cached"] == 3
    assert snapshot["cache_key_short"] == "aaaaaaaaaaaa"
    dashboard = live_dashboard_html(tmp_path)
    assert "本地多轨缓存命中" in dashboard
    assert "整首歌曲" in dashboard
    assert "GPU 未重复分离" in dashboard
    assert "命中 5 窗" in dashboard
    assert "本地已有 3 首完整歌曲缓存" in dashboard


def test_live_dashboard_shows_persistent_song_inventory_before_a_cache_hit(
    tmp_path: Path,
) -> None:
    (tmp_path / "gpu-status.json").write_text(
        json.dumps({"state": "waiting", "songs_cached": 3}),
        encoding="utf-8",
    )

    dashboard = live_dashboard_html(tmp_path)

    assert "最近一窗未命中" in dashboard
    assert "本地已有 3 首完整歌曲缓存" in dashboard


def test_live_dashboard_exposes_waiting_real_phone_acceptance(tmp_path: Path) -> None:
    (tmp_path / "acceptance-report.json").write_text(
        json.dumps(
            {
                "state": "waiting_for_phone",
                "passed": False,
                "requirements": {
                    "stream_received": False,
                    "gpu_first_play": False,
                    "song_cache_available": False,
                    "song_cache_replayed": False,
                    "zero_active_underruns": False,
                    "zero_active_device_recoveries": False,
                    "zero_active_skipped_sequences": False,
                    "mixer_adjusted_during_stream": False,
                    "mixer_latency_below_limit": False,
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)
    dashboard = live_dashboard_html(tmp_path)

    assert snapshot["acceptance_state"] == "waiting_for_phone"
    assert snapshot["acceptance_requirements"]["stream_received"] is False
    assert "真机自动验收：等待手机" in dashboard
    assert "手机音频 ○" in dashboard
    assert "播放中声卡零重连 ○" in dashboard
    assert "播放中分轨零跳窗 ○" in dashboard
    assert "播放中调音 ○" in dashboard


def test_live_dashboard_marks_real_phone_acceptance_passed(tmp_path: Path) -> None:
    requirements = {
        "stream_received": True,
        "gpu_first_play": True,
        "song_cache_available": True,
        "song_cache_replayed": True,
        "zero_active_underruns": True,
        "zero_active_device_recoveries": True,
        "zero_active_skipped_sequences": True,
        "mixer_adjusted_during_stream": True,
        "mixer_latency_below_limit": True,
    }
    (tmp_path / "acceptance-report.json").write_text(
        json.dumps(
            {
                "state": "passed",
                "passed": True,
                "requirements": requirements,
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)
    dashboard = live_dashboard_html(tmp_path)

    assert snapshot["acceptance_passed"] is True
    assert snapshot["acceptance_requirements"] == requirements
    assert "真机自动验收：全部通过" in dashboard
    assert "歌曲缓存重播 ✓" in dashboard
    assert "播放中零欠载 ✓" in dashboard
    assert "播放中声卡零重连 ✓" in dashboard
    assert "播放中分轨零跳窗 ✓" in dashboard


def test_live_dashboard_labels_cross_song_composite_cache_hit(tmp_path: Path) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "result-00000013.json").write_text(
        json.dumps(
            {
                "sequence": 13,
                "processing_seconds": 0.0,
                "cache_hit": True,
                "cache_scope": "song-composite",
                "cache_key": "b" * 64,
                "cache_part_count": 2,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "gpu-status.json").write_text(
        json.dumps({"state": "running", "last_sequence": 13}),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["cache_scope"] == "song-composite"
    dashboard = live_dashboard_html(tmp_path)
    assert "跨曲组合缓存命中 · 0 GPU" in dashboard
    assert "跨曲组合" in dashboard


def test_live_dashboard_reports_original_mix_fallback_without_claiming_gpu_success(
    tmp_path: Path,
) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "result-00000014.json").write_text(
        json.dumps(
            {
                "sequence": 14,
                "processing_seconds": 1.25,
                "cache_hit": False,
                "cache_scope": "fallback",
                "fallback_audio": True,
                "fallback_stem": "other",
                "error": "GPU temporary failure",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "gpu-status.json").write_text(
        json.dumps(
            {
                "state": "degraded",
                "last_sequence": 14,
                "fallback_windows": 2,
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["fallback_audio"] is True
    assert snapshot["fallback_windows"] == 2
    assert snapshot["fallback_error"] == "GPU temporary failure"
    dashboard = live_dashboard_html(tmp_path)
    assert "原声保底已接管" in dashboard
    assert "累计 2 窗" in dashboard
    assert "GPU 完成" not in dashboard
    assert "处理完成" in dashboard


def test_live_ui_defaults_follow_active_airplay_profile_and_mixer(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "state": "airplay_waiting",
                "input_source": "airplay",
                "profile_name": "六轨 · 加吉他/钢琴",
                "track_count": 6,
                "device_endpoint": "192.168.31.88:4010",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "gains": {
                    "vocals": 0.0,
                    "drums": 0.2,
                    "bass": 0.4,
                    "guitar": 0.6,
                    "piano": 0.8,
                    "other": 1.0,
                }
            }
        ),
        encoding="utf-8",
    )

    defaults = live_ui_defaults(tmp_path)

    assert defaults["input_source"] == "airplay"
    assert defaults["process_id"] is None
    assert defaults["profile_name"] == "六轨 · 加吉他/钢琴"
    assert defaults["device_endpoint"] == "192.168.31.88:4010"
    assert defaults["gains"]["vocals"] == 0.0
    assert defaults["gains"]["piano"] == 0.8


def test_live_ui_defaults_preserve_active_windows_process(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "process_id": 321,
                "profile_name": "四轨 · 人声/鼓/贝斯/其他",
                "track_count": 4,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")

    defaults = live_ui_defaults(tmp_path)

    assert defaults["input_source"] == "process"
    assert defaults["process_id"] == 321
    assert defaults["profile_name"] == "四轨 · 人声/鼓/贝斯/其他"


def test_live_ui_defaults_reject_stale_active_controller_state(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "state": "airplay_waiting",
                "input_source": "airplay",
                "profile_name": "六轨 · 加吉他/钢琴",
            }
        ),
        encoding="utf-8",
    )
    heartbeat = tmp_path / "controller-heartbeat.json"
    heartbeat.write_text("{}", encoding="utf-8")
    stale_time = time.time() - 10.0
    os.utime(heartbeat, (stale_time, stale_time))

    defaults = live_ui_defaults(tmp_path)

    assert defaults["input_source"] == "process"
    assert defaults["profile_name"] == "人声 / 伴奏 · 高质量"


def test_live_ui_defaults_ignore_stale_airplay_command_when_controller_stopped(
    tmp_path: Path,
) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "stopped"}),
        encoding="utf-8",
    )
    (tmp_path / "command.json").write_text(
        json.dumps(
            {
                "action": "start_airplay",
                "profile_name": "六轨 · 加吉他/钢琴",
            }
        ),
        encoding="utf-8",
    )

    defaults = live_ui_defaults(tmp_path)

    assert defaults["input_source"] == "process"
    assert defaults["profile_name"] == "人声 / 伴奏 · 高质量"


def test_live_dashboard_exposes_escaped_airplay_track_and_progress(tmp_path: Path) -> None:
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "codec": "ALAC",
                "track": {
                    "revision": 7,
                    "title": "Song <Live>",
                    "artist": "Artist & Guest",
                    "album": "Album",
                    "has_progress": True,
                    "position_seconds": 65.2,
                    "duration_seconds": 245.0,
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["track_revision"] == 7
    assert snapshot["track_title"] == "Song <Live>"
    assert snapshot["track_artist"] == "Artist & Guest"
    assert snapshot["track_position_seconds"] == 65.2
    assert snapshot["track_duration_seconds"] == 245.0
    dashboard = live_dashboard_html(tmp_path)
    assert "Song &lt;Live&gt;" in dashboard
    assert "Artist &amp; Guest" in dashboard
    assert "01:05 / 04:05" in dashboard
    assert "Song <Live>" not in dashboard


def test_live_dashboard_synchronizes_cached_lyrics_to_airplay_position(
    tmp_path: Path,
) -> None:
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "track": {
                    "revision": 7,
                    "title": "Song",
                    "artist": "Artist",
                    "position_seconds": 3.6,
                    "duration_seconds": 120,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "lyrics-status.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "source": "cache",
                "track": {"revision": 7, "title": "Song", "artist": "Artist"},
                "lines": [
                    {"time_seconds": 1.0, "text": "First"},
                    {"time_seconds": 3.5, "text": "Current <line>"},
                    {"time_seconds": 8.0, "text": "Next & line"},
                ],
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["lyrics_state"] == "ready"
    assert snapshot["lyrics_source"] == "cache"
    assert snapshot["lyrics_current"] == "Current <line>"
    assert snapshot["lyrics_previous"] == ["First"]
    assert snapshot["lyrics_upcoming"] == ["Next & line"]
    dashboard = live_dashboard_html(tmp_path)
    assert "同步歌词 · 本地缓存" in dashboard
    assert "Current &lt;line&gt;" in dashboard
    assert "Next &amp; line" in dashboard
    assert "stem-lyrics-current" in dashboard


def test_live_dashboard_renders_smooth_audio_reactive_orb_and_waveform(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(live_control_module.time, "monotonic", lambda: 0.0)
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "codec": "ALAC",
                "peak_left": 0.8,
                "peak_right": 0.6,
                "rms_left": 0.4,
                "rms_right": 0.2,
                "waveform": [index / 63 for index in range(64)],
            }
        ),
        encoding="utf-8",
    )

    dashboard = live_dashboard_html(tmp_path)

    assert 'class="stem-orb"' in dashboard
    assert 'role="img"' in dashboard
    assert 'aria-label="音频能量可视球，当前强度 44%"' in dashboard
    assert "stem-orb-core" in dashboard
    assert "stem-orb-halo" in dashboard
    assert "stem-orb-ring" in dashboard
    assert "@keyframes stem-orb-breathe" in dashboard
    assert "@keyframes stem-wave-flow" in dashboard
    assert "transform:scaleY(var(--wave-low))" in dashboard
    assert "animation-delay:-0.11s" in dashboard
    assert dashboard.count('<i style="--wave-low:') == 32
    assert "@media (prefers-reduced-motion:reduce)" in dashboard
    assert "animation:none!important" in dashboard


def test_live_dashboard_never_reuses_stale_lyrics_after_track_revision_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "track": {
                    "revision": 8,
                    "title": "New Song",
                    "artist": "Artist",
                    "position_seconds": 2,
                    "duration_seconds": 100,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "lyrics-status.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "source": "cache",
                "track": {"revision": 7, "title": "Old Song", "artist": "Artist"},
                "lines": [{"time_seconds": 1, "text": "Old lyric"}],
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["lyrics_state"] == "waiting"
    assert snapshot["lyrics_current"] == ""
    assert "Old lyric" not in live_dashboard_html(tmp_path)


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
        "track_count": 6,
    }
    assert not (tmp_path / "command.json.part").exists()
    system_sequence = write_command(tmp_path, "start", 0)
    system_payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert system_payload["sequence"] == system_sequence
    assert system_payload["process_id"] == 0
    with pytest.raises(ValueError, match="Windows 音频来源"):
        write_command(tmp_path, "start", -1)


def test_enable_music_watch_command_does_not_require_a_running_pid(
    tmp_path: Path,
) -> None:
    sequence = write_command(
        tmp_path,
        "enable_music_watch",
        monitor_stem="vocals",
        profile_name="六轨 · 加吉他/钢琴",
        hop_seconds=3,
    )

    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload["sequence"] == sequence
    assert payload["action"] == "enable_music_watch"
    assert payload["profile_name"] == "六轨 · 加吉他/钢琴"
    assert payload["track_count"] == 6
    assert payload["hop_seconds"] == 3
    assert "process_id" not in payload


def test_write_command_sequence_is_strictly_increasing_when_clock_repeats(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("stemstudio.live_control.time.time_ns", lambda: 123)
    first = write_command(tmp_path, "stop")
    second = write_command(tmp_path, "stop")
    assert first == 123
    assert second == 124


def test_mixer_sequence_is_strictly_increasing_when_clock_repeats(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("stemstudio.live_control.time.time_ns", lambda: 456)
    gains = {"vocals": 1.0, "instrumental": 1.0}
    first = write_mixer_control(tmp_path, "人声 / 伴奏 · 高质量", gains)
    second = write_mixer_control(tmp_path, "人声 / 伴奏 · 高质量", gains)
    assert first == 456
    assert second == 457


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


def test_write_command_starts_airplay_host_without_process_id(tmp_path: Path) -> None:
    sequence = write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="drums",
        profile_name="四轨 · 人声/鼓/贝斯/其他",
    )
    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload == {
        "sequence": sequence,
        "action": "start_airplay",
        "input_source": "airplay",
        "monitor_stem": "drums",
        "profile_name": "四轨 · 人声/鼓/贝斯/其他",
        "track_count": 4,
    }


def test_write_command_validates_and_publishes_device_endpoint(tmp_path: Path) -> None:
    sequence = write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="vocals",
        profile_name="人声 / 伴奏 · 高质量",
        device_endpoint=" 192.168.31.88:4010 ",
    )
    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload["sequence"] == sequence
    assert payload["device_endpoint"] == "192.168.31.88:4010"

    for invalid in (
        "speaker.local:4010",
        "192.168.031.88:4010",
        "192.168.31.88:0",
        "::1:4010",
    ):
        with pytest.raises(ValueError, match="设备地址"):
            write_command(tmp_path, "start_airplay", device_endpoint=invalid)


def test_write_command_publishes_validated_realtime_hop(tmp_path: Path) -> None:
    write_command(tmp_path, "start_airplay", hop_seconds=3)
    payload = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert payload["hop_seconds"] == 3

    with pytest.raises(ValueError, match="3 秒或 6 秒"):
        write_command(tmp_path, "start_airplay", hop_seconds=4)


def test_live_dashboard_exposes_device_network_queue_and_errors(tmp_path: Path) -> None:
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "device_network_enabled": True,
                "device_queue_packets": 7,
                "device_queue_capacity_packets": 128,
                "device_dropped_packets": 3,
                "device_dropped_frames": 660,
                "device_sent_packets": 900,
                "device_sent_bytes": 123456,
                "device_send_errors": 2,
                "device_last_socket_error": 10051,
            }
        ),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)
    assert snapshot["device_network_enabled"] is True
    assert snapshot["device_queue_packets"] == 7
    assert snapshot["device_dropped_frames"] == 660
    assert snapshot["device_send_errors"] == 2
    dashboard = live_dashboard_html(tmp_path)
    assert "黄山派网络" in dashboard
    assert "队列 7/128 包" in dashboard
    assert "丢弃 660 帧" in dashboard
    assert "Winsock 10051" in dashboard


def test_active_stem_visibility_follows_selected_profile() -> None:
    visibility = active_stem_visibility("四轨 · 人声/鼓/贝斯/其他")
    assert visibility == {
        "vocals": True,
        "instrumental": False,
        "drums": True,
        "bass": True,
        "guitar": False,
        "piano": False,
        "other": True,
    }


def test_write_mixer_control_is_atomic_complete_and_ordered(tmp_path: Path) -> None:
    sequence = write_mixer_control(
        tmp_path,
        "人声 / 伴奏 · 高质量",
        {"vocals": 0.75, "instrumental": 0.0},
    )

    assert (tmp_path / "mixer-control-2.tsv").read_text(encoding="utf-8") == (
        "stem-studio-mixer-v1\n"
        f"sequence\t{sequence}\n"
        "vocals\t0.750000\n"
        "instrumental\t0.000000\n"
    )
    assert not (tmp_path / "mixer-control-2.tsv.part").exists()


def test_write_mixer_control_retries_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch
) -> None:
    real_replace = live_control_module.os.replace
    attempts = 0
    waits: list[float] = []

    def flaky_replace(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(live_control_module.os, "replace", flaky_replace)
    monkeypatch.setattr(live_control_module.time, "sleep", waits.append)

    write_mixer_control(
        tmp_path,
        "人声 / 伴奏 · 高质量",
        {"vocals": 0.25, "instrumental": 0.75},
    )

    assert attempts == 3
    assert waits == [0.002, 0.002]
    assert (tmp_path / "mixer-control-2.tsv").is_file()
    assert not (tmp_path / "mixer-control-2.tsv.part").exists()


def test_write_mixer_control_cleans_partial_file_after_persistent_sharing_violation(
    tmp_path: Path, monkeypatch
) -> None:
    def blocked_replace(_source, _destination) -> None:
        raise PermissionError("simulated persistent sharing violation")

    monkeypatch.setattr(live_control_module.os, "replace", blocked_replace)
    monkeypatch.setattr(live_control_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistent"):
        write_mixer_control(
            tmp_path,
            "人声 / 伴奏 · 高质量",
            {"vocals": 0.25, "instrumental": 0.75},
        )

    assert not (tmp_path / "mixer-control-2.tsv.part").exists()


@pytest.mark.parametrize(
    ("gains", "message"),
    [
        ({"vocals": 1.0}, "缺少"),
        ({"vocals": -0.01, "instrumental": 1.0}, "0 到 1"),
        ({"vocals": float("nan"), "instrumental": 1.0}, "有限"),
    ],
)
def test_write_mixer_control_rejects_incomplete_or_invalid_snapshots(
    tmp_path: Path, gains: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        write_mixer_control(tmp_path, "人声 / 伴奏 · 高质量", gains)


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
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    (tmp_path / "gpu-status.json").write_text(
        json.dumps({"state": "running", "last_sequence": 3}), encoding="utf-8"
    )
    status = status_markdown(tmp_path)
    assert "PID 42" in status
    assert "窗口 3" in status


def test_status_markdown_reports_airplay_waiting_and_streaming(tmp_path: Path) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "airplay_waiting", "profile_name": "四轨 · 人声/鼓/贝斯/其他"}),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    (tmp_path / "airplay-status.json").write_text(
        json.dumps({"state": "waiting", "enabled": True, "receiver": "Stem Studio"}),
        encoding="utf-8",
    )
    assert "等待手机 AirPlay" in status_markdown(tmp_path)

    (tmp_path / "airplay-status.json").write_text(
        json.dumps(
            {
                "state": "streaming",
                "enabled": True,
                "codec": "ALAC",
                "pcm_frames": 529200,
                "published_windows": 2,
                "receiver": "Stem Studio",
            }
        ),
        encoding="utf-8",
    )
    status = status_markdown(tmp_path)
    assert "AirPlay PCM 正在接收" in status
    assert "ALAC" in status
    assert "窗口 2" in status


def test_active_controller_status_without_a_fresh_heartbeat_is_reported_offline(
    tmp_path: Path,
) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "airplay_waiting", "profile_name": "六轨 · 加吉他/钢琴"}),
        encoding="utf-8",
    )
    heartbeat = tmp_path / "controller-heartbeat.json"
    heartbeat.write_text(json.dumps({"version": 1, "pid": 42}), encoding="utf-8")
    stale_time = time.time() - 10.0
    os.utime(heartbeat, (stale_time, stale_time))
    (tmp_path / "airplay-status.json").write_text(
        json.dumps({"state": "waiting", "enabled": True, "receiver": "Stem Studio"}),
        encoding="utf-8",
    )

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["controller_online"] is False
    assert snapshot["controller_stalled"] is True
    assert snapshot["controller_heartbeat_age_seconds"] >= 9.0
    assert "控制器心跳已停止" in status_markdown(tmp_path)
    assert "控制器已离线" in live_dashboard_html(tmp_path)


def test_fresh_controller_heartbeat_keeps_active_controller_online(
    tmp_path: Path,
) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps({"state": "airplay_waiting"}),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")

    snapshot = live_pipeline_snapshot(tmp_path)

    assert snapshot["controller_online"] is True
    assert snapshot["controller_stalled"] is False
    assert snapshot["controller_heartbeat_age_seconds"] < 1.0
