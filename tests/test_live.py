import json
import wave
from pathlib import Path

import pytest

from stemstudio.core import LIVE_PROFILES
from stemstudio.live import (
    InvalidLiveTransition,
    LiveChunkProcessor,
    LiveConfig,
    LiveSessionState,
    LiveStateMachine,
    PersistentSeparator,
    discover_ready_chunks,
)


def _write_pcm16_stereo(path: Path, frames: int, sample_rate: int = 44_100) -> None:
    left = (1000).to_bytes(2, "little", signed=True)
    right = (-1000).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes((left + right) * frames)


def _write_constant_pcm16_stereo(
    path: Path,
    frames: int,
    value: int,
    sample_rate: int,
) -> None:
    sample = value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes((sample + sample) * frames)


def _first_pcm16_sample(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        return int.from_bytes(audio.readframes(1)[:2], "little", signed=True)


def test_live_config_defaults_match_high_quality_streaming_contract() -> None:
    config = LiveConfig()

    assert config.sample_rate == 44_100
    assert config.channels == 2
    assert config.window_seconds == 12
    assert config.hop_seconds == 6
    assert config.stable_offset_seconds == 0
    assert config.crossfade_milliseconds == 100
    assert config.window_frames == 529_200
    assert config.hop_frames == 264_600
    assert config.overlap_frames == 4_410
    assert config.output_frames == 269_010


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_rate": 0}, "采样率"),
        ({"channels": 1}, "双声道"),
        ({"window_seconds": 4}, "窗口"),
        ({"hop_seconds": 0}, "步长"),
        ({"hop_seconds": 5}, "整除"),
        ({"stable_offset_seconds": 11}, "稳定区间"),
        ({"crossfade_milliseconds": 0}, "交叉淡化"),
        ({"crossfade_milliseconds": 7_000}, "交叉淡化"),
    ],
)
def test_live_config_rejects_invalid_stream_geometry(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LiveConfig(**kwargs)


def test_discover_ready_chunks_orders_sequence_and_ignores_partial_files(tmp_path: Path) -> None:
    for name in [
        "capture-00000003.wav",
        "capture-00000001.wav",
        "capture-00000002.part",
        "capture-invalid.wav",
        "notes.txt",
    ]:
        (tmp_path / name).write_bytes(b"data")

    chunks = discover_ready_chunks(tmp_path)

    assert [(chunk.sequence, chunk.path.name) for chunk in chunks] == [
        (1, "capture-00000001.wav"),
        (3, "capture-00000003.wav"),
    ]


def test_discover_ready_chunks_skips_already_processed_sequences(tmp_path: Path) -> None:
    for sequence in range(1, 5):
        (tmp_path / f"capture-{sequence:08d}.wav").write_bytes(b"data")

    chunks = discover_ready_chunks(tmp_path, after_sequence=2)

    assert [chunk.sequence for chunk in chunks] == [3, 4]


def test_live_state_machine_happy_path() -> None:
    machine = LiveStateMachine()

    machine.start(process_id=4242, process_name="Music.exe")
    assert machine.snapshot().state is LiveSessionState.warming
    assert machine.snapshot().process_id == 4242

    machine.mark_running(buffered_seconds=12.0)
    assert machine.snapshot().state is LiveSessionState.running
    assert machine.snapshot().buffered_seconds == 12.0

    machine.update_progress(sequence=7, latency_seconds=9.4, buffered_seconds=4.0)
    snapshot = machine.snapshot()
    assert snapshot.last_sequence == 7
    assert snapshot.latency_seconds == 9.4
    assert snapshot.buffered_seconds == 4.0

    machine.stop()
    snapshot = machine.snapshot()
    assert snapshot.state is LiveSessionState.stopped
    assert snapshot.process_id is None
    assert snapshot.last_sequence is None


def test_live_state_machine_failure_is_reported_and_can_stop() -> None:
    machine = LiveStateMachine()
    machine.start(process_id=7, process_name="Player.exe")

    machine.fail("GPU 显存不足")
    snapshot = machine.snapshot()
    assert snapshot.state is LiveSessionState.error
    assert snapshot.error == "GPU 显存不足"

    machine.stop()
    assert machine.snapshot().state is LiveSessionState.stopped


def test_live_state_machine_rejects_invalid_transitions() -> None:
    machine = LiveStateMachine()

    with pytest.raises(InvalidLiveTransition):
        machine.mark_running(buffered_seconds=1.0)

    machine.start(process_id=5, process_name="Player.exe")
    with pytest.raises(InvalidLiveTransition):
        machine.start(process_id=6, process_name="Other.exe")


def test_live_state_machine_rejects_invalid_process() -> None:
    machine = LiveStateMachine()

    with pytest.raises(ValueError, match="进程"):
        machine.start(process_id=0, process_name="")


def test_persistent_separator_loads_model_only_once(tmp_path: Path) -> None:
    instances = []

    class FakeSeparator:
        def __init__(self, **kwargs):
            self.output_dir = kwargs["output_dir"]
            self.loaded = []
            instances.append(self)

        def load_model(self, model_filename: str) -> None:
            self.loaded.append(model_filename)

        def separate(self, source: str) -> list[str]:
            return ["Vocals.wav", "Instrumental.wav"]

    separator = PersistentSeparator(
        model_dir=tmp_path / "models",
        work_dir=tmp_path / "work",
        model_filename="quality.ckpt",
        separator_factory=FakeSeparator,
    )

    first = separator.separate(tmp_path / "one.wav")
    second = separator.separate(tmp_path / "two.wav")

    assert len(instances) == 1
    assert instances[0].loaded == ["quality.ckpt"]
    assert [path.name for path in first] == ["Vocals.wav", "Instrumental.wav"]
    assert [path.name for path in second] == ["Vocals.wav", "Instrumental.wav"]


@pytest.mark.parametrize("shifts", [1, 2])
def test_persistent_separator_configures_requested_demucs_shifts_for_live(
    tmp_path: Path, shifts: int
) -> None:
    captured = {}

    class FakeSeparator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def load_model(self, model_filename: str) -> None:
            captured["model_filename"] = model_filename

    PersistentSeparator(
        model_dir=tmp_path / "models",
        work_dir=tmp_path / "work",
        model_filename="htdemucs_6s.yaml",
        demucs_shifts=shifts,
        separator_factory=FakeSeparator,
    )

    assert captured["demucs_params"]["shifts"] == shifts
    assert captured["demucs_params"]["overlap"] == 0.25
    assert captured["use_soundfile"] is True


def test_persistent_separator_rejects_unsupported_live_shifts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shifts"):
        PersistentSeparator(
            model_dir=tmp_path / "models",
            work_dir=tmp_path / "work",
            model_filename="htdemucs_6s.yaml",
            demucs_shifts=3,
            separator_factory=lambda **_kwargs: None,
        )


def test_persistent_separator_keeps_demucs_network_resident_between_windows(
    tmp_path: Path,
) -> None:
    architecture = object()
    runtime_instances = []
    fallback_calls = []

    class FakeSeparator:
        def __init__(self, **_kwargs):
            self.model_instance = None

        def load_model(self, model_filename: str) -> None:
            assert model_filename == "htdemucs_6s.yaml"
            self.model_instance = architecture

        def separate(self, source: str) -> list[str]:
            fallback_calls.append(source)
            return []

    class FakeResidentDemucsRuntime:
        def __init__(self, model_architecture) -> None:
            assert model_architecture is architecture
            self.calls = []
            self.closed = False
            runtime_instances.append(self)

        def separate(self, source: Path) -> list[Path]:
            self.calls.append(source.name)
            return [Path("Vocals.wav"), Path("Other.wav")]

        def close(self) -> None:
            self.closed = True

    separator = PersistentSeparator(
        model_dir=tmp_path / "models",
        work_dir=tmp_path / "work",
        model_filename="htdemucs_6s.yaml",
        separator_factory=FakeSeparator,
        resident_demucs_factory=FakeResidentDemucsRuntime,
    )

    first = separator.separate(tmp_path / "one.wav")
    second = separator.separate(tmp_path / "two.wav")
    separator.close()

    assert len(runtime_instances) == 1
    assert runtime_instances[0].calls == ["one.wav", "two.wav"]
    assert runtime_instances[0].closed is True
    assert fallback_calls == []
    assert [path.name for path in first] == ["Vocals.wav", "Other.wav"]
    assert [path.name for path in second] == ["Vocals.wav", "Other.wav"]


def test_live_chunk_processor_extracts_stable_hop_and_publishes_manifest_last(
    tmp_path: Path,
) -> None:
    config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    work = tmp_path / "work"
    inbox.mkdir()
    source = inbox / "capture-00000001.wav"
    _write_pcm16_stereo(source, config.window_frames, config.sample_rate)

    class FakeEngine:
        def separate(self, _source: Path) -> list[Path]:
            work.mkdir(parents=True, exist_ok=True)
            vocals = work / "track_(Vocals).wav"
            instrumental = work / "track_(Instrumental).wav"
            _write_pcm16_stereo(vocals, config.window_frames, config.sample_rate)
            _write_pcm16_stereo(instrumental, config.window_frames, config.sample_rate)
            return [vocals, instrumental]

    result = LiveChunkProcessor(config, outbox, FakeEngine()).process(
        discover_ready_chunks(inbox)[0]
    )

    assert result.sequence == 1
    assert result.manifest.name == "result-00000001.json"
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["sequence"] == 1
    assert manifest["version"] == 2
    assert manifest["hop_seconds"] == config.hop_seconds
    assert manifest["overlap_frames"] == config.overlap_frames
    assert manifest["cache_hit"] is False
    assert set(manifest["stems"]) == {"vocals", "instrumental"}
    for filename in manifest["stems"].values():
        output = outbox / filename
        assert output.exists()
        with wave.open(str(output), "rb") as audio:
            assert audio.getnframes() == config.output_frames
            assert audio.getframerate() == config.sample_rate
            assert audio.getnchannels() == 2
    assert not list(outbox.glob("*.part"))


def test_live_chunk_processor_rejects_missing_stem(tmp_path: Path) -> None:
    source = tmp_path / "capture-00000001.wav"
    _write_pcm16_stereo(source, 80, 10)

    class IncompleteEngine:
        def separate(self, _source: Path) -> list[Path]:
            only = tmp_path / "track_(Vocals).wav"
            _write_pcm16_stereo(only, 80, 10)
            return [only]

    processor = LiveChunkProcessor(
        LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3),
        tmp_path / "out",
        IncompleteEngine(),
    )

    with pytest.raises(RuntimeError, match="人声和伴奏"):
        processor.process(discover_ready_chunks(tmp_path)[0])


def test_live_chunk_processor_publishes_all_six_requested_stems(tmp_path: Path) -> None:
    config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)
    source = tmp_path / "capture-00000001.wav"
    _write_pcm16_stereo(source, config.window_frames, config.sample_rate)
    expected = ("vocals", "drums", "bass", "guitar", "piano", "other")

    class SixStemEngine:
        def separate(self, _source: Path) -> list[Path]:
            outputs = []
            for stem in expected:
                path = tmp_path / f"track_({stem.title()}).wav"
                _write_pcm16_stereo(path, config.window_frames, config.sample_rate)
                outputs.append(path)
            return outputs

    result = LiveChunkProcessor(
        config,
        tmp_path / "out",
        SixStemEngine(),
        expected_stems=expected,
    ).process(discover_ready_chunks(tmp_path)[0])

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert tuple(manifest["stems"]) == expected
    assert all((tmp_path / "out" / filename).is_file() for filename in manifest["stems"].values())


def test_live_chunk_processor_materializes_returned_near_silent_sources(
    tmp_path: Path,
) -> None:
    profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)
    source = tmp_path / "capture-00000001.wav"
    _write_pcm16_stereo(source, config.window_frames, config.sample_rate)

    class NearSilentSixStemEngine:
        def separate(self, _source: Path) -> list[Path]:
            outputs = []
            for stem in ("vocals", "drums", "bass", "guitar", "piano", "other"):
                path = tmp_path / f"track_({stem.title()}).wav"
                if stem in {"drums", "other"}:
                    _write_constant_pcm16_stereo(
                        path,
                        config.window_frames,
                        1_000,
                        config.sample_rate,
                    )
                outputs.append(path)
            return outputs

    result = LiveChunkProcessor(
        config,
        tmp_path / "out",
        NearSilentSixStemEngine(),
        expected_stems=profile.stems,
        stem_sources=dict(zip(profile.stems, profile.source_groups, strict=True)),
    ).process(discover_ready_chunks(tmp_path)[0])

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    vocals = tmp_path / "out" / manifest["stems"]["vocals"]
    instrumental = tmp_path / "out" / manifest["stems"]["instrumental"]
    assert _first_pcm16_sample(vocals) == 0
    assert _first_pcm16_sample(instrumental) == 2_000


@pytest.mark.parametrize(
    ("profile_name", "expected_samples"),
    [
        ("人声 / 伴奏 · 高质量", {"vocals": 1_000, "instrumental": 20_000}),
        (
            "四轨 · 人声/鼓/贝斯/其他",
            {"vocals": 1_000, "drums": 2_000, "bass": 3_000, "other": 15_000},
        ),
    ],
)
def test_live_chunk_processor_composes_fast_six_source_model_into_requested_tracks(
    tmp_path: Path,
    profile_name: str,
    expected_samples: dict[str, int],
) -> None:
    profile = LIVE_PROFILES[profile_name]
    config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)
    source = tmp_path / "capture-00000001.wav"
    _write_pcm16_stereo(source, config.window_frames, config.sample_rate)
    source_values = {
        "vocals": 1_000,
        "drums": 2_000,
        "bass": 3_000,
        "guitar": 4_000,
        "piano": 5_000,
        "other": 6_000,
    }

    class SixStemEngine:
        def separate(self, _source: Path) -> list[Path]:
            outputs = []
            for stem, value in source_values.items():
                path = tmp_path / f"track_({stem.title()}).wav"
                _write_constant_pcm16_stereo(
                    path,
                    config.window_frames,
                    value,
                    config.sample_rate,
                )
                outputs.append(path)
            return outputs

    result = LiveChunkProcessor(
        config,
        tmp_path / "out",
        SixStemEngine(),
        expected_stems=profile.stems,
        stem_sources=dict(zip(profile.stems, profile.source_groups, strict=True)),
    ).process(discover_ready_chunks(tmp_path)[0])

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    actual = {
        stem: _first_pcm16_sample(tmp_path / "out" / filename)
        for stem, filename in manifest["stems"].items()
    }
    assert actual == expected_samples


def test_live_chunk_processor_saturates_composed_track_without_wrapping(tmp_path: Path) -> None:
    profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)
    source = tmp_path / "capture-00000001.wav"
    _write_pcm16_stereo(source, config.window_frames, config.sample_rate)

    class LoudSixStemEngine:
        def separate(self, _source: Path) -> list[Path]:
            outputs = []
            for stem in ("vocals", "drums", "bass", "guitar", "piano", "other"):
                path = tmp_path / f"track_({stem.title()}).wav"
                _write_constant_pcm16_stereo(
                    path,
                    config.window_frames,
                    10_000,
                    config.sample_rate,
                )
                outputs.append(path)
            return outputs

    result = LiveChunkProcessor(
        config,
        tmp_path / "out",
        LoudSixStemEngine(),
        expected_stems=profile.stems,
        stem_sources=dict(zip(profile.stems, profile.source_groups, strict=True)),
    ).process(discover_ready_chunks(tmp_path)[0])

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    instrumental = tmp_path / "out" / manifest["stems"]["instrumental"]
    assert _first_pcm16_sample(instrumental) == 32_767
