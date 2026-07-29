import json
import os
import struct
import threading
import wave
from pathlib import Path

from stemstudio.core import LIVE_PROFILES
from stemstudio.inference_process import InferenceDeadlineExceeded, InferenceWarmingUp
from stemstudio.live import LiveConfig
from stemstudio.live_control import write_command
from stemstudio.live_worker import (
    LiveWorker,
    last_published_sequence,
    prune_live_artifacts,
    start_live_worker,
)
from stemstudio.song_cache import SongCache, SongCacheProfile, SongTrackMetadata


def _write_audio(path: Path, frames: int, sample_rate: int = 10) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00\xff\xff" * frames)


def _unique_pcm(frames: range, scale: int = 1) -> bytes:
    samples = []
    for frame in frames:
        value = frame * scale - 1_000
        samples.extend((value, -value))
    return struct.pack(f"<{len(samples)}h", *samples)


def _write_pcm(path: Path, pcm: bytes, sample_rate: int = 10) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm)


def test_last_published_sequence_ignores_partial_and_invalid_files(tmp_path: Path) -> None:
    for name in ["result-00000002.json", "result-00000007.json", "result-00000009.json.part", "other.json"]:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert last_published_sequence(tmp_path) == 7


def test_live_worker_does_not_load_gpu_model_until_a_chunk_is_ready(tmp_path: Path) -> None:
    calls = []
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: calls.append(True))
    assert worker.process_available() == []
    assert calls == []


def test_start_live_worker_forwards_dynamic_inference_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    created: list[tuple[float, int, int, float]] = []

    class FakeWorker:
        def __init__(
            self,
            _root,
            *,
            inference_timeout_seconds: float,
            live_hop_seconds: int,
            demucs_shifts: int,
            shifts_benchmark_limit_seconds: float,
        ) -> None:
            created.append(
                (
                    inference_timeout_seconds,
                    live_hop_seconds,
                    demucs_shifts,
                    shifts_benchmark_limit_seconds,
                )
            )

        def run(self, stop_event: threading.Event) -> None:
            stop_event.set()

    monkeypatch.setattr("stemstudio.live_worker.LiveWorker", FakeWorker)

    thread, stop_event = start_live_worker(
        tmp_path,
        inference_timeout_seconds=2.7,
        live_hop_seconds=3,
        demucs_shifts=2,
        shifts_benchmark_limit_seconds=2.4,
    )
    thread.join(timeout=1.0)

    assert stop_event.is_set()
    assert created == [(2.7, 3, 2, 2.4)]


def test_live_worker_status_is_atomically_parseable(tmp_path: Path) -> None:
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: None)
    worker._write_status({"state": "waiting", "last_sequence": 3})
    payload = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert payload["state"] == "waiting"
    assert payload["last_sequence"] == 3
    assert payload["model_state"] == "stopped"
    assert payload["inference_timeout_seconds"] == 5.5
    assert not (tmp_path / "gpu-status.json.part").exists()


def test_live_worker_status_retries_transient_windows_replace_and_skips_unchanged_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: None)
    real_replace = os.replace
    calls = 0

    def transient_replace(source, destination) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("status file is temporarily open")
        real_replace(source, destination)

    monkeypatch.setattr("stemstudio.live_worker.os.replace", transient_replace)

    worker._write_status({"state": "waiting", "last_sequence": 3})
    worker._write_status({"state": "waiting", "last_sequence": 3})

    assert calls == 3
    payload = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert payload["state"] == "waiting"
    assert payload["last_sequence"] == 3


def test_live_worker_prewarms_profile_from_start_command_before_audio_arrives(tmp_path: Path) -> None:
    loaded_profiles = []
    worker = LiveWorker(tmp_path, separator_factory=lambda profile: loaded_profiles.append(profile))
    command_sequence = write_command(
        tmp_path,
        "start",
        42,
        monitor_stem="piano",
        profile_name="六轨 · 加吉他/钢琴",
    )
    (tmp_path / "capture-session.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "command_sequence": command_sequence,
                "initial_sequence": 1,
            }
        ),
        encoding="utf-8",
    )

    worker.process_available()

    assert worker.active_profile.name == "六轨 · 加吉他/钢琴"
    assert loaded_profiles == [LIVE_PROFILES["六轨 · 加吉他/钢琴"]]


def test_live_worker_prewarms_airplay_profile_before_audio_arrives(tmp_path: Path) -> None:
    loaded_profiles = []
    worker = LiveWorker(tmp_path, separator_factory=lambda profile: loaded_profiles.append(profile))
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="drums",
        profile_name="四轨 · 人声/鼓/贝斯/其他",
    )

    worker.process_available()

    assert worker.active_profile.name == "四轨 · 人声/鼓/贝斯/其他"
    assert loaded_profiles == [LIVE_PROFILES["四轨 · 人声/鼓/贝斯/其他"]]
    worker._write_status({})
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "waiting"
    assert status["profile_name"] == "四轨 · 人声/鼓/贝斯/其他"


def test_live_worker_refreshes_inference_observability_without_audio(tmp_path: Path) -> None:
    stop_event = threading.Event()

    class ObservableSeparator:
        def status_snapshot(self) -> dict:
            stop_event.set()
            return {
                "model_state": "ready",
                "inference_process_pid": 4321,
                "inference_timeout_seconds": 5.5,
                "model_warmup_seconds": 8.25,
                "inference_error": None,
            }

        def close(self) -> None:
            pass

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: ObservableSeparator())
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="vocals",
        profile_name="六轨 · 加吉他/钢琴",
    )

    worker.run(stop_event, poll_seconds=0.0)

    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["model_state"] == "ready"
    assert status["inference_process_pid"] == 4321
    assert status["inference_timeout_seconds"] == 5.5
    assert status["model_warmup_seconds"] == 8.25
    assert status["warmup_windows"] == 0
    assert status["deadline_windows"] == 0
    assert status["max_processing_seconds"] == 0.0


def test_live_worker_stop_command_releases_preheated_model_process(tmp_path: Path) -> None:
    class ClosableSeparator:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    separator = ClosableSeparator()
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: separator)
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="vocals",
        profile_name="人声 / 伴奏 · 高质量",
    )
    worker.process_available()
    assert separator.close_count == 0

    write_command(tmp_path, "stop")
    worker.process_available()

    assert separator.close_count == 1


def test_live_worker_reuses_resident_model_when_switching_realtime_output_groups(
    tmp_path: Path,
) -> None:
    created = []

    class ReusableSeparator:
        def __init__(self) -> None:
            self.close_count = 0
            created.append(self)

        def close(self) -> None:
            self.close_count += 1

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: ReusableSeparator())
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="other",
        profile_name="六轨 · 加吉他/钢琴",
    )
    worker.process_available()
    resident = created[0]

    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="instrumental",
        profile_name="人声 / 伴奏 · 高质量",
    )
    worker.process_available()

    assert created == [resident]
    assert resident.close_count == 0
    assert worker._processor is not None
    assert worker._processor.separator is resident
    assert worker._processor.expected_stems == ("vocals", "instrumental")


def test_live_worker_keeps_warming_model_alive_while_publishing_original_audio(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)

    class WarmingSeparator:
        def __init__(self) -> None:
            self.close_count = 0

        def separate(self, _source: Path) -> list[Path]:
            raise InferenceWarmingUp("model prewarming")

        def close(self) -> None:
            self.close_count += 1

    separator = WarmingSeparator()
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: separator)
    worker.config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)

    results = worker.process_available()

    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_audio"] is True
    assert manifest["fallback_reason"] == "model_warmup"
    assert separator.close_count == 0


def test_live_worker_terminates_timed_out_model_and_keeps_audio_timeline(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)

    class TimedOutSeparator:
        def __init__(self) -> None:
            self.close_count = 0

        def separate(self, _source: Path) -> list[Path]:
            raise InferenceDeadlineExceeded("GPU inference exceeded 5.5 seconds")

        def close(self) -> None:
            self.close_count += 1

    separator = TimedOutSeparator()
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: separator)
    worker.config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)

    results = worker.process_available()

    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_audio"] is True
    assert manifest["fallback_reason"] == "inference_deadline"
    assert separator.close_count == 1
    assert worker._deadline_windows == 1
    assert worker._warmup_windows == 0
    assert worker._max_processing_seconds >= manifest["processing_seconds"] - 0.001


def test_windows_start_waits_for_matching_capture_session_and_resets_sequence(
    tmp_path: Path,
) -> None:
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: None)
    worker._last_sequence = 4503
    command_sequence = write_command(
        tmp_path,
        "start",
        42,
        profile_name="人声 / 伴奏 · 高质量",
        hop_seconds=3,
    )

    worker._sync_requested_profile()
    assert worker._session_active is False
    assert worker._last_sequence == 4503

    (tmp_path / "capture-session.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "command_sequence": command_sequence,
                "initial_sequence": 1,
            }
        ),
        encoding="utf-8",
    )
    worker._sync_requested_profile()

    assert worker._session_active is True
    assert worker._last_sequence == 0


def test_live_worker_falls_back_to_original_mix_and_continues_with_next(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    for sequence in (1, 2):
        _write_audio(inbox / f"capture-{sequence:08d}.wav", 80)
    (inbox / "capture-00000001.json").write_text("{}", encoding="utf-8")

    class FlakySeparator:
        def separate(self, source: Path) -> list[Path]:
            if source.name == "capture-00000001.wav":
                raise FileNotFoundError("missing generated vocal stem")
            outputs = []
            for stem in ("Vocals", "Instrumental"):
                output = tmp_path / "work" / f"track_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_audio(output, 80)
                outputs.append(output)
            return outputs

    created = []

    def create_separator(_profile):
        created.append(True)
        return FlakySeparator()

    worker = LiveWorker(tmp_path, separator_factory=create_separator)
    worker.config = LiveConfig(sample_rate=10, window_seconds=8, hop_seconds=2, stable_offset_seconds=3)

    first_results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in first_results] == [1]
    assert (tmp_path / "failed" / "capture-00000001.wav").is_file()
    assert (tmp_path / "failed" / "capture-00000001.json").is_file()
    failed_manifest = json.loads(
        (tmp_path / "outbox" / "result-00000001.json").read_text(encoding="utf-8")
    )
    assert failed_manifest["sequence"] == 1
    assert failed_manifest["fallback_audio"] is True
    assert failed_manifest["fallback_stem"] == "instrumental"
    assert set(failed_manifest["stems"]) == {"vocals", "instrumental"}
    assert "missing generated vocal stem" in failed_manifest["error"]
    with wave.open(
        str(tmp_path / "outbox" / failed_manifest["stems"]["vocals"]),
        "rb",
    ) as vocals:
        assert vocals.getnframes() == worker.config.output_frames
        assert vocals.readframes(vocals.getnframes()) == b"\x00" * (
            worker.config.output_frames * worker.config.channels * 2
        )
    with wave.open(
        str(tmp_path / "outbox" / failed_manifest["stems"]["instrumental"]),
        "rb",
    ) as instrumental:
        assert instrumental.getnframes() == worker.config.output_frames
        assert instrumental.readframes(instrumental.getnframes()) == (
            b"\x01\x00\xff\xff" * worker.config.output_frames
        )

    results = worker.process_available(max_chunks=1)
    assert [result.sequence for result in results] == [2]
    assert len(created) == 2


def test_six_track_failure_fallback_keeps_original_mix_in_other_stem(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    capture = inbox / "capture-00000001.wav"
    _write_pcm(capture, _unique_pcm(range(80)))
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="other",
        profile_name="六轨 · 加吉他/钢琴",
    )

    def fail_to_load(_profile):
        raise RuntimeError("GPU unavailable")

    worker = LiveWorker(tmp_path, separator_factory=fail_to_load)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_audio"] is True
    assert manifest["fallback_stem"] == "other"
    assert set(manifest["stems"]) == {
        "vocals",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
    }
    for stem, filename in manifest["stems"].items():
        with wave.open(str(tmp_path / "outbox" / filename), "rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        expected = (
            _unique_pcm(range(30, 51))
            if stem == "other"
            else b"\x00" * worker.config.output_frames * worker.config.channels * 2
        )
        assert pcm == expected


def test_failure_fallback_stays_on_designated_stem_when_its_fader_is_muted(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)
    write_command(
        tmp_path,
        "start_airplay",
        monitor_stem="other",
        profile_name="六轨 · 加吉他/钢琴",
    )
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "gains": {
                    "vocals": 0.8,
                    "drums": 0.2,
                    "bass": 0.4,
                    "guitar": 0.6,
                    "piano": 0.0,
                    "other": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )

    def fail_to_load(_profile):
        raise RuntimeError("GPU unavailable")

    worker = LiveWorker(tmp_path, separator_factory=fail_to_load)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    result = worker.process_available(max_chunks=1)[0]
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

    assert manifest["fallback_stem"] == "other"
    assert manifest["fallback_output_gain"] == 1.0


def test_live_worker_status_counts_fallback_windows_as_degraded_but_playable(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)
    stop_event = threading.Event()

    def fail_and_stop(_profile):
        stop_event.set()
        raise RuntimeError("temporary GPU failure")

    worker = LiveWorker(tmp_path, separator_factory=fail_and_stop)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    worker.run(stop_event, poll_seconds=0.0)

    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "degraded"
    assert status["last_sequence"] == 1
    assert status["fallback_audio"] is True
    assert status["fallback_windows"] == 1
    assert status["recovering"] is True


def test_live_worker_uses_original_mix_to_drain_realtime_backlog_before_gpu(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    for sequence in (1, 2):
        _write_audio(inbox / f"capture-{sequence:08d}.wav", 80)
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "state": "playing",
                "sequence": 1,
                "queued_sequence": 0,
                "buffered_seconds": 3.0,
                "prebuffer_seconds": 12.0,
                "gains": {"vocals": 1.0, "instrumental": 1.0},
            }
        ),
        encoding="utf-8",
    )
    model_loads: list[str] = []
    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: model_loads.append("loaded"),
        inference_timeout_seconds=5.5,
    )
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    assert model_loads == []
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_audio"] is True
    assert manifest["fallback_reason"] == "realtime_backlog"
    assert "实时积压" in manifest["error"]
    assert (inbox / "capture-00000002.wav").is_file()


def test_realtime_backlog_fallback_keeps_resident_separator_alive(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    for sequence in (1, 2):
        _write_audio(inbox / f"capture-{sequence:08d}.wav", 80)
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "state": "playing",
                "buffered_seconds": 3.0,
                "gains": {"vocals": 1.0, "instrumental": 1.0},
            }
        ),
        encoding="utf-8",
    )

    class ResidentSeparator:
        def __init__(self) -> None:
            self.close_count = 0

        def separate(self, _source: Path) -> list[Path]:
            raise AssertionError("backlog fallback must run before inference")

        def close(self) -> None:
            self.close_count += 1

    separator = ResidentSeparator()
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: separator)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )
    worker._processor = worker._create_processor(separator)

    result = worker.process_available(max_chunks=1)[0]

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_reason"] == "realtime_backlog"
    assert worker._processor is not None
    assert separator.close_count == 0


def test_silent_windows_capture_keeps_resident_separator_alive(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)
    (tmp_path / "capture-input-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "source": "windows",
                "signal_detected": False,
            }
        ),
        encoding="utf-8",
    )

    class ResidentSeparator:
        def __init__(self) -> None:
            self.close_count = 0

        def separate(self, _source: Path) -> list[Path]:
            raise AssertionError("silent input must not enter inference")

        def close(self) -> None:
            self.close_count += 1

    separator = ResidentSeparator()
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: separator)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )
    worker._processor = worker._create_processor(separator)

    result = worker.process_available(max_chunks=1)[0]

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_reason"] == "input_silence"
    assert worker._processor is not None
    assert separator.close_count == 0


def test_live_worker_uses_original_mix_before_gpu_when_buffer_is_below_deadline_reserve(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)
    (tmp_path / "playback-status.json").write_text(
        json.dumps(
            {
                "state": "playing",
                "sequence": 1,
                "queued_sequence": 0,
                "buffered_seconds": 2.0,
                "gains": {"vocals": 1.0, "instrumental": 1.0},
            }
        ),
        encoding="utf-8",
    )
    model_loads: list[str] = []
    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: model_loads.append("loaded"),
        inference_timeout_seconds=5.5,
    )
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    assert model_loads == []
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["fallback_audio"] is True
    assert manifest["fallback_reason"] == "low_buffer_reserve"
    assert "安全余量" in manifest["error"]
    worker._write_status({})
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["low_buffer_fallback_windows"] == 1
    assert status["continuity_reserve_seconds"] == 3.0


def test_live_worker_reuses_content_cache_without_running_separator_again(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    first = inbox / "capture-00000001.wav"
    _write_audio(first, 80)

    calls: list[str] = []
    model_loads: list[str] = []

    class CountingSeparator:
        def separate(self, source: Path) -> list[Path]:
            calls.append(source.name)
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"track_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_audio(output, 80)
                outputs.append(output)
            return outputs

    def load_separator(_profile):
        model_loads.append("loaded")
        return CountingSeparator()

    worker = LiveWorker(tmp_path, separator_factory=load_separator)
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    first_result = worker.process_available(max_chunks=1)
    assert [result.sequence for result in first_result] == [1]
    assert calls == ["capture-00000001.wav"]
    assert model_loads == ["loaded"]

    second = inbox / "capture-00000002.wav"
    _write_audio(second, 80)
    restarted_worker = LiveWorker(tmp_path, separator_factory=load_separator)
    restarted_worker.config = worker.config
    second_result = restarted_worker.process_available(max_chunks=1)

    assert [result.sequence for result in second_result] == [2]
    assert calls == ["capture-00000001.wav"]
    assert model_loads == ["loaded"]
    manifest = json.loads(
        (tmp_path / "outbox" / "result-00000002.json").read_text(encoding="utf-8")
    )
    assert manifest["cache_hit"] is True
    assert manifest["processing_seconds"] == 0.0
    assert set(manifest["stems"]) == {"vocals", "instrumental"}
    assert list((tmp_path / "cache").glob("*/*/manifest.json"))


def test_windows_live_capture_bypasses_window_cache_to_protect_deadline(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    _write_audio(inbox / "capture-00000001.wav", 80)
    (tmp_path / "capture-input-status.json").write_text(
        json.dumps(
            {
                "state": "capturing",
                "source": "windows",
                "signal_detected": True,
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    class CountingSeparator:
        def separate(self, source: Path) -> list[Path]:
            calls.append(source.name)
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"track_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_audio(output, 80)
                outputs.append(output)
            return outputs

    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: CountingSeparator(),
    )
    worker.config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )

    assert [result.sequence for result in worker.process_available()] == [1]
    _write_audio(inbox / "capture-00000002.wav", 80)
    restarted = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: CountingSeparator(),
    )
    restarted.config = worker.config

    assert [result.sequence for result in restarted.process_available()] == [2]
    assert calls == ["capture-00000001.wav", "capture-00000002.wav"]
    assert not list((tmp_path / "cache").glob("*/*/manifest.json"))


def test_live_worker_uses_song_cache_at_non_window_aligned_position(tmp_path: Path) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )
    live_profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    cache_profile = SongCacheProfile(
        profile_name=live_profile.name,
        model_filename=live_profile.model_filename,
        stems=live_profile.stems,
        sample_rate=10,
        channels=2,
        bits_per_sample=16,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
        overlap_frames=config.overlap_frames,
    )
    metadata = SongTrackMetadata(
        title="Cached Song",
        artist="Artist",
        album="Album",
        duration_frames=80,
        sample_rate=10,
    )
    cache = SongCache(tmp_path / "song-cache")
    builder = cache.start_build("first-play", cache_profile)
    builder.append(
        stream_start_frame=0,
        track_start_frame=0,
        metadata=metadata,
        source_pcm=_unique_pcm(range(80)),
        stems={
            "vocals": _unique_pcm(range(80), 2),
            "instrumental": _unique_pcm(range(80), 3),
        },
    )
    assert builder.finalize() is not None

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    capture = inbox / "capture-00000001.wav"
    _write_pcm(capture, _unique_pcm(range(80)))
    anchor = {
        "revision": 1,
        "metadata_revision": 1,
        "has_progress": True,
        "start_rtp": 0,
        "current_rtp": 0,
        "end_rtp": 80,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 80,
        "title": "Cached Song",
        "artist": "Artist",
        "album": "Album",
    }
    capture.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "airplay",
                "sequence": 1,
                "sample_rate": 10,
                "stream_start_frame": 0,
                "stream_end_frame": 80,
                "track": anchor,
                "anchors": [anchor],
            }
        ),
        encoding="utf-8",
    )
    model_loads = []
    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: model_loads.append("loaded"),
    )
    worker.config = config

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    assert model_loads == []
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["cache_hit"] is True
    assert manifest["cache_scope"] == "song"
    assert manifest["cached_start_frame"] == 30


def test_live_worker_replays_after_stale_airplay_duration_and_anchor_resync_without_gpu(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    live_profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    cache_profile = SongCacheProfile(
        profile_name=live_profile.name,
        model_filename=live_profile.model_filename,
        stems=live_profile.stems,
        sample_rate=10,
        channels=2,
        bits_per_sample=16,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
        overlap_frames=config.overlap_frames,
    )
    cached_metadata = SongTrackMetadata(
        title="AirPlay Resync Song",
        artist="Artist",
        album="Album",
        duration_frames=80,
        sample_rate=10,
    )
    cache = SongCache(tmp_path / "song-cache")
    builder = cache.start_build("first-play", cache_profile)
    builder.append(
        stream_start_frame=0,
        track_start_frame=0,
        metadata=cached_metadata,
        source_pcm=_unique_pcm(range(80)),
        stems={
            "vocals": _unique_pcm(range(80), 2),
            "instrumental": _unique_pcm(range(80), 3),
        },
    )
    assert builder.finalize() is not None

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    capture = inbox / "capture-00000001.wav"
    _write_pcm(capture, _unique_pcm(range(30, 110)))
    replay_anchor = {
        "revision": 2,
        "metadata_revision": 2,
        "has_progress": True,
        "start_rtp": 10_000,
        "current_rtp": 10_000,
        "end_rtp": 10_120,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 120,
        "title": "AirPlay Resync Song",
        "artist": "Artist",
        "album": "Album",
    }
    capture.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "airplay",
                "sequence": 1,
                "sample_rate": 10,
                "stream_start_frame": 0,
                "stream_end_frame": 80,
                "track": replay_anchor,
                "anchors": [replay_anchor],
            }
        ),
        encoding="utf-8",
    )
    model_loads: list[str] = []
    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: model_loads.append("loaded"),
    )
    worker.config = config

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    assert model_loads == []
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["cache_hit"] is True
    assert manifest["cache_scope"] == "song"
    assert manifest["processing_seconds"] == 0.0
    assert manifest["cached_start_frame"] == 30


def test_live_worker_composes_cached_tracks_across_song_boundary_without_gpu(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
    )
    live_profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
    cache_profile = SongCacheProfile(
        profile_name=live_profile.name,
        model_filename=live_profile.model_filename,
        stems=live_profile.stems,
        sample_rate=10,
        channels=2,
        bits_per_sample=16,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=3,
        overlap_frames=config.overlap_frames,
    )
    cache = SongCache(tmp_path / "song-cache")

    def cache_song(title: str, source_scale: int, vocal_scale: int) -> None:
        metadata = SongTrackMetadata(
            title=title,
            artist="Artist",
            album="Album",
            duration_frames=60,
            sample_rate=10,
        )
        builder = cache.start_build(title, cache_profile)
        builder.append(
            stream_start_frame=0,
            track_start_frame=0,
            metadata=metadata,
            source_pcm=_unique_pcm(range(60), source_scale),
            stems={
                "vocals": _unique_pcm(range(60), vocal_scale),
                "instrumental": _unique_pcm(range(60), vocal_scale + 1),
            },
        )
        assert builder.finalize() is not None

    cache_song("First Song", source_scale=1, vocal_scale=3)
    cache_song("Second Song", source_scale=2, vocal_scale=4)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    capture = inbox / "capture-00000001.wav"
    _write_pcm(
        capture,
        _unique_pcm(range(22, 60), 1) + _unique_pcm(range(42), 2),
    )

    def anchor(
        revision: int,
        stream_frame: int,
        track_frame: int,
        title: str,
    ) -> dict:
        return {
            "revision": revision,
            "metadata_revision": revision,
            "has_progress": True,
            "start_rtp": 0,
            "current_rtp": track_frame,
            "end_rtp": 60,
            "anchor_stream_frame": stream_frame,
            "track_position_frame": track_frame,
            "track_duration_frame": 60,
            "title": title,
            "artist": "Artist",
            "album": "Album",
        }

    first = anchor(1, stream_frame=0, track_frame=22, title="First Song")
    second = anchor(2, stream_frame=38, track_frame=0, title="Second Song")
    capture.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "airplay",
                "sequence": 1,
                "sample_rate": 10,
                "stream_start_frame": 0,
                "stream_end_frame": 80,
                "track": second,
                "anchors": [first, second],
            }
        ),
        encoding="utf-8",
    )

    model_loads: list[str] = []
    worker = LiveWorker(
        tmp_path,
        separator_factory=lambda _profile: model_loads.append("loaded"),
    )
    worker.config = config

    results = worker.process_available(max_chunks=1)

    assert [result.sequence for result in results] == [1]
    assert model_loads == []
    manifest = json.loads(results[0].manifest.read_text(encoding="utf-8"))
    assert manifest["cache_hit"] is True
    assert manifest["cache_scope"] == "song-composite"
    assert manifest["cache_part_count"] == 2
    with wave.open(str(tmp_path / "outbox" / manifest["stems"]["vocals"]), "rb") as audio:
        assert audio.getnframes() == 21
        assert audio.readframes(21) == (
            _unique_pcm(range(52, 60), 3)
            + _unique_pcm(range(13), 4)
        )


def test_live_worker_builds_continuous_song_cache_then_replays_without_gpu(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def anchor(revision: int, stream_frame: int, track_frame: int, title: str) -> dict:
        return {
            "revision": revision,
            "metadata_revision": revision,
            "has_progress": True,
            "start_rtp": 0,
            "current_rtp": track_frame,
            "end_rtp": 40,
            "anchor_stream_frame": stream_frame,
            "track_position_frame": track_frame,
            "track_duration_frame": 40,
            "title": title,
            "artist": "Artist",
            "album": "Album",
        }

    def write_capture(
        sequence: int,
        stream_start: int,
        pcm: bytes,
        anchors: list[dict],
    ) -> None:
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, pcm)
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": anchors[-1],
                    "anchors": anchors,
                }
            ),
            encoding="utf-8",
        )

    first_song = anchor(1, 0, 0, "First Song")
    next_song = anchor(2, 40, 0, "Next Song")
    write_capture(1, 0, _unique_pcm(range(0, 80)), [first_song, next_song])
    write_capture(2, 20, _unique_pcm(range(20, 100)), [first_song, next_song])

    calls: list[str] = []

    class DerivedSeparator:
        def separate(self, source: Path) -> list[Path]:
            calls.append(source.name)
            with wave.open(str(source), "rb") as audio:
                source_frames = audio.readframes(audio.getnframes())
            samples = struct.unpack(f"<{len(source_frames) // 2}h", source_frames)
            outputs = []
            for stem, scale in (
                ("Vocals", 2),
                ("Drums", 3),
                ("Bass", 0),
                ("Guitar", 0),
                ("Piano", 0),
                ("Other", 0),
            ):
                scaled = struct.pack(
                    f"<{len(samples)}h",
                    *(max(-32_768, min(32_767, sample * scale)) for sample in samples),
                )
                output = tmp_path / "work" / f"track_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, scaled)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: DerivedSeparator())
    worker.config = config

    first_results = worker.process_available(max_chunks=2)

    assert [result.sequence for result in first_results] == [1, 2]
    assert calls == ["capture-00000001.wav", "capture-00000002.wav"]
    assert list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert not list((tmp_path / "cache").glob("*/*/manifest.json"))

    repeated_song = anchor(3, 100, 0, "First Song")
    after_repeat = anchor(4, 140, 0, "After Repeat")
    write_capture(
        3,
        100,
        _unique_pcm(range(0, 80)),
        [repeated_song, after_repeat],
    )

    replay = worker.process_available(max_chunks=1)

    assert [result.sequence for result in replay] == [3]
    assert calls == ["capture-00000001.wav", "capture-00000002.wav"]
    replay_manifest = json.loads(replay[0].manifest.read_text(encoding="utf-8"))
    assert replay_manifest["cache_hit"] is True
    assert replay_manifest["cache_scope"] == "song"
    with wave.open(
        str(tmp_path / "outbox" / replay_manifest["stems"]["vocals"]),
        "rb",
    ) as audio:
        source_samples = struct.unpack(
            "<42h",
            _unique_pcm(range(0, 21)),
        )
        expected = struct.pack("<42h", *(sample * 2 for sample in source_samples))
        assert audio.getnframes() == 21
        assert audio.readframes(21) == expected

    stopped = threading.Event()
    stopped.set()
    restarted = LiveWorker(tmp_path, separator_factory=lambda _profile: None)
    restarted.run(stopped, poll_seconds=0.0)
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["songs_cached"] == 1


def test_live_worker_restarts_same_revision_song_cache_after_backward_seek(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def anchor(stream_frame: int, track_frame: int) -> dict:
        return {
            "revision": 1,
            "metadata_revision": 1,
            "has_progress": True,
            "start_rtp": 0,
            "current_rtp": track_frame,
            "end_rtp": 40,
            "anchor_stream_frame": stream_frame,
            "track_position_frame": track_frame,
            "track_duration_frame": 40,
            "title": "Seekable Song",
            "artist": "Artist",
            "album": "Album",
        }

    def write_capture(
        sequence: int,
        stream_start: int,
        pcm: bytes,
        anchors: list[dict],
    ) -> None:
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, pcm)
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": anchors[-1],
                    "anchors": anchors,
                }
            ),
            encoding="utf-8",
        )

    middle = anchor(0, 20)
    restart = anchor(20, 0)
    write_capture(1, 0, _unique_pcm(range(20, 100)), [middle])
    write_capture(2, 20, _unique_pcm(range(0, 80)), [middle, restart])
    write_capture(3, 40, _unique_pcm(range(20, 100)), [middle, restart])

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"seek_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    results = worker.process_available(max_chunks=3)

    assert [result.sequence for result in results] == [1, 2, 3]
    manifests = list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    source = manifests[0].parent / manifest["files"]["source"]["filename"]
    with wave.open(str(source), "rb") as audio:
        assert audio.getnframes() == 40
        assert audio.readframes(40) == _unique_pcm(range(0, 40))


def test_live_worker_continues_song_cache_across_progress_revision_resync(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original = {
        "revision": 1,
        "metadata_revision": 1,
        "has_progress": True,
        "start_rtp": 100,
        "current_rtp": 100,
        "end_rtp": 140,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 40,
        "title": "Progress Refreshed Song",
        "artist": "Artist",
        "album": "Album",
    }
    refreshed = {
        **original,
        "revision": 2,
        "current_rtp": 124,
        "end_rtp": 141,
        "anchor_stream_frame": 25,
        "track_position_frame": 24,
        "track_duration_frame": 41,
        "title": "Upcoming Track Metadata Arrived Early",
        "artist": "Next Artist",
        "album": "Next Album",
    }
    for sequence, stream_start, track, anchors in (
        (1, 0, original, [original]),
        (2, 20, refreshed, [original, refreshed]),
    ):
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, _unique_pcm(range(stream_start, stream_start + 80)))
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": track,
                    "anchors": anchors,
                }
            ),
            encoding="utf-8",
        )

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"revision_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    results = worker.process_available(max_chunks=2)

    assert [result.sequence for result in results] == [1, 2]
    manifests = list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["metadata"]["title"] == "Progress Refreshed Song"
    assert manifest["metadata"]["artist"] == "Artist"
    source = manifests[0].parent / manifest["files"]["source"]["filename"]
    with wave.open(str(source), "rb") as audio:
        assert audio.getnframes() == 40
        assert audio.readframes(40) == _unique_pcm(range(0, 40))
    worker._write_status({})
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["last_song_cache_outcome"]["state"] == "stored"
    assert status["last_song_cache_outcome"]["track_revision"] == 2
    assert status["last_song_cache_outcome"]["track_start_rtp"] == 100


def test_live_worker_clears_transient_missing_metadata_cache_error_after_recovery(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    missing_metadata = {
        "revision": 1,
        "metadata_revision": 0,
        "has_progress": True,
        "start_rtp": 100,
        "current_rtp": 100,
        "end_rtp": 140,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 40,
        "title": "",
        "artist": "",
        "album": "",
    }
    recovered_metadata = {
        **missing_metadata,
        "revision": 2,
        "metadata_revision": 1,
        "current_rtp": 120,
        "anchor_stream_frame": 20,
        "track_position_frame": 20,
        "title": "Recovered Song",
        "artist": "Artist",
        "album": "Album",
    }
    for sequence, stream_start, track, anchors in (
        (1, 0, missing_metadata, [missing_metadata]),
        (2, 20, recovered_metadata, [missing_metadata, recovered_metadata]),
    ):
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, _unique_pcm(range(stream_start, stream_start + 80)))
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": track,
                    "anchors": anchors,
                }
            ),
            encoding="utf-8",
        )

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"metadata_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    first = worker.process_available(max_chunks=1)
    assert [result.sequence for result in first] == [1]
    assert worker._last_cache_error == "歌曲缓存至少需要标题或艺术家。"

    second = worker.process_available(max_chunks=1)
    worker._write_status({})
    recovered_status = json.loads(
        (tmp_path / "gpu-status.json").read_text(encoding="utf-8")
    )
    assert [result.sequence for result in second] == [2]
    assert recovered_status["song_builder_frame_count"] > 0
    assert worker._last_cache_error is None


def test_live_worker_completes_song_cache_when_track_ends_mid_hop(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    anchor = {
        "revision": 1,
        "metadata_revision": 1,
        "has_progress": True,
        "start_rtp": 0,
        "current_rtp": 0,
        "end_rtp": 35,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 35,
        "title": "Mid-hop Ending",
        "artist": "Artist",
        "album": "Album",
    }
    for sequence, stream_start in ((1, 0), (2, 20)):
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, _unique_pcm(range(stream_start, stream_start + 80)))
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": anchor,
                    "anchors": [anchor],
                }
            ),
            encoding="utf-8",
        )

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"ending_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    results = worker.process_available(max_chunks=2)

    assert [result.sequence for result in results] == [1, 2]
    manifests = list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    source = manifests[0].parent / manifest["files"]["source"]["filename"]
    with wave.open(str(source), "rb") as audio:
        assert audio.getnframes() == 35
        assert audio.readframes(35) == _unique_pcm(range(0, 35))


def test_live_worker_recovers_song_prefix_when_airplay_anchor_arrives_late(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old_anchor = {
        "revision": 1,
        "metadata_revision": 1,
        "has_progress": True,
        "start_rtp": 0,
        "current_rtp": 0,
        "end_rtp": 1_000,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 1_000,
        "title": "Old Song",
        "artist": "Artist",
        "album": "Album",
    }
    late_new_anchor = {
        "revision": 2,
        "metadata_revision": 2,
        "has_progress": True,
        "start_rtp": 2_000,
        "current_rtp": 2_000,
        "end_rtp": 2_040,
        "anchor_stream_frame": 15,
        "track_position_frame": 0,
        "track_duration_frame": 40,
        "title": "Late Anchor Song",
        "artist": "Artist",
        "album": "Album",
    }
    for sequence, stream_start, anchor in (
        (1, 0, old_anchor),
        (2, 20, late_new_anchor),
        (3, 40, late_new_anchor),
    ):
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, _unique_pcm(range(stream_start, stream_start + 80)))
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": anchor,
                    "anchors": [anchor],
                }
            ),
            encoding="utf-8",
        )

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"late_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    results = worker.process_available(max_chunks=3)

    assert [result.sequence for result in results] == [1, 2, 3]
    manifests = list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["metadata"]["title"] == "Late Anchor Song"
    source = manifests[0].parent / manifest["files"]["source"]["filename"]
    with wave.open(str(source), "rb") as audio:
        assert audio.getnframes() == 40
        assert audio.readframes(40) == _unique_pcm(range(15, 55))
    worker._write_status({})
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["song_prefix_recoveries"] == 1
    assert status["song_prefix_recovery_misses"] == 0
    assert status["last_song_cache_outcome"]["state"] == "stored"
    assert status["last_song_cache_outcome"]["first_track_start_frame"] == 0


def test_live_worker_recovers_song_prefix_from_before_anchor_in_current_hop(
    tmp_path: Path,
) -> None:
    config = LiveConfig(
        sample_rate=10,
        window_seconds=8,
        hop_seconds=2,
        stable_offset_seconds=0,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old_anchor = {
        "revision": 1,
        "metadata_revision": 1,
        "has_progress": True,
        "start_rtp": 0,
        "current_rtp": 0,
        "end_rtp": 1_000,
        "anchor_stream_frame": 0,
        "track_position_frame": 0,
        "track_duration_frame": 1_000,
        "title": "Old Song",
        "artist": "Artist",
        "album": "Album",
    }
    in_hop_anchor = {
        "revision": 2,
        "metadata_revision": 2,
        "has_progress": True,
        "start_rtp": 2_000,
        "current_rtp": 2_005,
        "end_rtp": 2_040,
        "anchor_stream_frame": 25,
        "track_position_frame": 5,
        "track_duration_frame": 40,
        "title": "In-hop Anchor Song",
        "artist": "Artist",
        "album": "Album",
    }
    captures = (
        (1, 0, old_anchor, [old_anchor]),
        (2, 20, in_hop_anchor, [old_anchor, in_hop_anchor]),
        (3, 40, in_hop_anchor, [in_hop_anchor]),
    )
    for sequence, stream_start, track, anchors in captures:
        capture = inbox / f"capture-{sequence:08d}.wav"
        _write_pcm(capture, _unique_pcm(range(stream_start, stream_start + 80)))
        capture.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "airplay",
                    "sequence": sequence,
                    "sample_rate": 10,
                    "stream_start_frame": stream_start,
                    "stream_end_frame": stream_start + 80,
                    "track": track,
                    "anchors": anchors,
                }
            ),
            encoding="utf-8",
        )

    class CopySeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            outputs = []
            for stem in ("Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"):
                output = tmp_path / "work" / f"in_hop_({stem}).wav"
                output.parent.mkdir(exist_ok=True)
                _write_pcm(output, pcm)
                outputs.append(output)
            return outputs

    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: CopySeparator())
    worker.config = config

    results = worker.process_available(max_chunks=3)

    assert [result.sequence for result in results] == [1, 2, 3]
    manifests = list((tmp_path / "song-cache" / "entries").glob("*/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    source = manifests[0].parent / manifest["files"]["source"]["filename"]
    with wave.open(str(source), "rb") as audio:
        assert audio.getnframes() == 40
        assert audio.readframes(40) == _unique_pcm(range(20, 60))
    worker._write_status({})
    status = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert status["song_prefix_recoveries"] == 1
    assert status["song_prefix_recovery_misses"] == 0
    assert status["last_song_cache_outcome"]["state"] == "stored"


def test_prune_live_artifacts_keeps_recent_and_uncommitted_files(tmp_path: Path) -> None:
    directories = {
        "inbox": "capture-{sequence:08d}.wav",
        "inbox_annotation": "capture-{sequence:08d}.json",
        "outbox_manifest": "result-{sequence:08d}.json",
        "outbox_stem": "result-{sequence:08d}-vocals.wav",
        "work": "capture-{sequence:08d}_(Vocals)_model.wav",
        "failed": "capture-{sequence:08d}.wav",
        "failed_annotation": "capture-{sequence:08d}.json",
    }
    for directory in ("inbox", "outbox", "work", "failed"):
        (tmp_path / directory).mkdir(parents=True)
    for sequence in range(1, 13):
        for kind, template in directories.items():
            directory = (
                "outbox" if kind.startswith("outbox")
                else "inbox" if kind.startswith("inbox")
                else "failed" if kind.startswith("failed")
                else kind
            )
            (tmp_path / directory / template.format(sequence=sequence)).write_bytes(b"audio")
    (tmp_path / "outbox" / "result-00000001.json.part").write_bytes(b"partial")
    (tmp_path / "inbox" / "capture-00000001.json.part").write_bytes(b"partial")
    (tmp_path / "inbox" / "capture-00000001.wav.pending").write_bytes(b"partial")
    (tmp_path / "work" / "model-cache.bin").write_bytes(b"keep")

    removed = prune_live_artifacts(tmp_path, safe_sequence=12, keep_sequences=3)

    assert removed == {"inbox": 18, "outbox": 18, "work": 9, "failed": 18}
    for sequence in (10, 11, 12):
        assert (tmp_path / "inbox" / f"capture-{sequence:08d}.wav").is_file()
        assert (tmp_path / "inbox" / f"capture-{sequence:08d}.json").is_file()
        assert (tmp_path / "outbox" / f"result-{sequence:08d}.json").is_file()
        assert (tmp_path / "outbox" / f"result-{sequence:08d}-vocals.wav").is_file()
    assert (tmp_path / "outbox" / "result-00000001.json.part").is_file()
    assert (tmp_path / "inbox" / "capture-00000001.json.part").is_file()
    assert (tmp_path / "inbox" / "capture-00000001.wav.pending").is_file()
    assert (tmp_path / "work" / "model-cache.bin").is_file()


def test_live_worker_prunes_only_sequences_copied_into_playback_queue(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir(parents=True)
    outbox.mkdir(parents=True)
    for sequence in range(1, 13):
        (inbox / f"capture-{sequence:08d}.wav").write_bytes(b"capture")
        (outbox / f"result-{sequence:08d}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "playback-status.json").write_text(
        json.dumps({"queued_sequence": 12}),
        encoding="utf-8",
    )
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: None)

    assert worker.process_available() == []

    assert not (inbox / "capture-00000004.wav").exists()
    assert (inbox / "capture-00000005.wav").is_file()
    assert not (outbox / "result-00000004.json").exists()
    assert (outbox / "result-00000005.json").is_file()
