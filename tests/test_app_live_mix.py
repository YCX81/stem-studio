from __future__ import annotations

from pathlib import Path

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
    assert not (tmp_path / "mixer-control.tsv").exists()
