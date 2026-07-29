from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import stemstudio.app as app_module
from stemstudio.live_control import write_mixer_percentages


def test_update_live_mix_publishes_all_active_stems_as_one_snapshot(
    tmp_path: Path,
) -> None:
    write_mixer_percentages(
        tmp_path,
        "四轨 · 人声/鼓/贝斯/其他",
        {
            "vocals": 75,
            "instrumental": 10,
            "drums": 50,
            "bass": 25,
            "guitar": 20,
            "piano": 30,
            "other": 0,
        },
    )

    contents = (tmp_path / "mixer-control-4.tsv").read_text(encoding="utf-8")
    assert "vocals\t0.750000" in contents
    assert "drums\t0.500000" in contents
    assert "bass\t0.250000" in contents
    assert "other\t0.000000" in contents
    assert "instrumental\t" not in contents
    assert "guitar\t" not in contents
    assert "piano\t" not in contents


def test_write_mixer_percentages_rejects_invalid_ui_range(
    tmp_path: Path,
) -> None:
    try:
        write_mixer_percentages(
            tmp_path,
            "人声 / 伴奏 · 高质量",
            {"vocals": 101, "instrumental": 35},
        )
    except ValueError as exc:
        assert "0 到 100" in str(exc)
    else:
        raise AssertionError("out-of-range mixer percentage was accepted")


def test_write_mixer_percentages_converts_two_track_snapshot(tmp_path: Path) -> None:
    write_mixer_percentages(
        tmp_path,
        "人声 / 伴奏 · 高质量",
        {"vocals": 80, "instrumental": 35},
    )

    assert "vocals\t0.800000" in (tmp_path / "mixer-control-2.tsv").read_text(
        encoding="utf-8"
    )
    assert "instrumental\t0.350000" in (
        tmp_path / "mixer-control-2.tsv"
    ).read_text(encoding="utf-8")


def test_profile_mixer_snapshots_are_isolated_from_other_ui_profiles(
    tmp_path: Path,
) -> None:
    write_mixer_percentages(
        tmp_path,
        "六轨 · 加吉他/钢琴",
        {
            "vocals": 0,
            "drums": 20,
            "bass": 40,
            "guitar": 60,
            "piano": 80,
            "other": 100,
        },
    )
    six_track_before = (tmp_path / "mixer-control-6.tsv").read_bytes()

    write_mixer_percentages(
        tmp_path,
        "人声 / 伴奏 · 高质量",
        {"vocals": 75, "instrumental": 25},
    )

    assert (tmp_path / "mixer-control-6.tsv").read_bytes() == six_track_before
    assert "instrumental\t0.250000" in (
        tmp_path / "mixer-control-2.tsv"
    ).read_text(encoding="utf-8")


def test_page_can_enable_music_monitor_without_selecting_a_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(
        app_module,
        "_hardware_config",
        lambda: SimpleNamespace(live_hop_seconds=3),
    )

    message = app_module.enable_music_auto_monitor(
        "六轨 · 加吉他/钢琴",
        "",
        100,
        0,
        100,
        100,
        100,
        100,
        100,
    )

    command = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert command["action"] == "enable_music_watch"
    assert command["track_count"] == 6
    assert "process_id" not in command
    assert "自动监控已启用" in message
    assert not (tmp_path / "mixer-control.tsv").exists()


def test_profile_selection_restarts_an_active_native_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "process_id": 0,
                "profile_name": "人声 / 伴奏 · 高质量",
                "track_count": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "controller-heartbeat.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(
        app_module,
        "_hardware_config",
        lambda: SimpleNamespace(live_hop_seconds=3),
    )
    monkeypatch.setattr(app_module, "_LIVE_RESERVATION", object())

    app_module.switch_live_profile(
        "六轨 · 加吉他/钢琴",
        100,
        0,
        100,
        100,
        100,
        100,
        100,
    )

    command = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert command["action"] == "start"
    assert command["process_id"] == 0
    assert command["profile_name"] == "六轨 · 加吉他/钢琴"
    assert command["track_count"] == 6
    assert command["hop_seconds"] == 3
    assert (tmp_path / "mixer-control-6.tsv").is_file()
