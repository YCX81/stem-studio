from pathlib import Path

import pytest

from stemstudio.core import (
    LIVE_PROFILES,
    MODEL_PROFILES,
    SeparationRequest,
    normalize_engine_outputs,
)


def test_live_profiles_define_models_and_monitorable_stems() -> None:
    two_track = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    assert two_track.stems == ("vocals", "instrumental")
    assert dict(zip(two_track.stems, two_track.source_groups, strict=True)) == {
        "vocals": ("vocals",),
        "instrumental": ("drums", "bass", "guitar", "piano", "other"),
    }
    assert LIVE_PROFILES["四轨 · 人声/鼓/贝斯/其他"].stems == (
        "vocals",
        "drums",
        "bass",
        "other",
    )
    assert LIVE_PROFILES["六轨 · 加吉他/钢琴"].stems == (
        "vocals",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
    )
    assert dict(
        zip(
            LIVE_PROFILES["四轨 · 人声/鼓/贝斯/其他"].stems,
            LIVE_PROFILES["四轨 · 人声/鼓/贝斯/其他"].source_groups,
            strict=True,
        )
    )["other"] == ("guitar", "piano", "other")
    assert {profile.model_filename for profile in LIVE_PROFILES.values()} == {
        MODEL_PROFILES["六轨 · 加吉他/钢琴"]
    }


def test_live_profile_labels_do_not_misrepresent_offline_quality_models() -> None:
    assert [profile.display_name for profile in LIVE_PROFILES.values()] == [
        "二轨 · 实时人声/伴奏",
        "四轨 · 实时人声/鼓/贝斯/其他",
        "六轨 · 实时完整分轨",
    ]


def test_request_accepts_supported_audio_and_creates_job_folder(tmp_path: Path) -> None:
    source = tmp_path / "我的歌曲.flac"
    source.write_bytes(b"audio")

    request = SeparationRequest.create(
        source=source,
        profile_name="人声 / 伴奏 · 高质量",
        output_format="FLAC",
        output_root=tmp_path / "outputs",
    )

    assert request.source == source.resolve()
    assert request.model_filename == MODEL_PROFILES["人声 / 伴奏 · 高质量"]
    assert request.output_format == "FLAC"
    assert request.output_dir.parent == (tmp_path / "outputs").resolve()
    assert request.output_dir.name.startswith("我的歌曲-")


@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".m4a", ".ogg", ".aac"])
def test_request_accepts_common_audio_formats(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / f"track{suffix}"
    source.write_bytes(b"audio")
    request = SeparationRequest.create(source, "四轨 · 人声/鼓/贝斯/其他", "WAV", tmp_path)
    assert request.source == source.resolve()


def test_request_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="音频文件不存在"):
        SeparationRequest.create(tmp_path / "missing.wav", "人声 / 伴奏 · 高质量", "FLAC", tmp_path)


def test_request_rejects_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not audio", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的音频格式"):
        SeparationRequest.create(source, "人声 / 伴奏 · 高质量", "FLAC", tmp_path)


def test_request_rejects_unknown_profile_and_format(tmp_path: Path) -> None:
    source = tmp_path / "track.wav"
    source.write_bytes(b"audio")
    with pytest.raises(ValueError, match="未知分离模式"):
        SeparationRequest.create(source, "unknown", "FLAC", tmp_path)
    with pytest.raises(ValueError, match="输出格式"):
        SeparationRequest.create(source, "人声 / 伴奏 · 高质量", "EXE", tmp_path)


def test_normalize_engine_outputs_resolves_relative_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "job"
    output_dir.mkdir()
    vocal = output_dir / "song_(Vocals).flac"
    instrumental = output_dir / "song_(Instrumental).flac"
    vocal.write_bytes(b"vocal")
    instrumental.write_bytes(b"instrumental")

    result = normalize_engine_outputs(
        [vocal.name, str(instrumental.resolve())], output_dir
    )

    assert result == [vocal.resolve(), instrumental.resolve()]


def test_normalize_engine_outputs_rejects_missing_result(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="没有生成"):
        normalize_engine_outputs(["missing.flac"], tmp_path)
