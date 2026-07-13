import json
import wave
from pathlib import Path

from stemstudio.live import LiveConfig
from stemstudio.live_control import write_command
from stemstudio.live_worker import LiveWorker, last_published_sequence


def _write_audio(path: Path, frames: int, sample_rate: int = 10) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00\xff\xff" * frames)


def test_last_published_sequence_ignores_partial_and_invalid_files(tmp_path: Path) -> None:
    for name in ["result-00000002.json", "result-00000007.json", "result-00000009.json.part", "other.json"]:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert last_published_sequence(tmp_path) == 7


def test_live_worker_does_not_load_gpu_model_until_a_chunk_is_ready(tmp_path: Path) -> None:
    calls = []
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: calls.append(True))
    assert worker.process_available() == []
    assert calls == []


def test_live_worker_status_is_atomically_parseable(tmp_path: Path) -> None:
    worker = LiveWorker(tmp_path, separator_factory=lambda _profile: None)
    worker._write_status({"state": "waiting", "last_sequence": 3})
    payload = json.loads((tmp_path / "gpu-status.json").read_text(encoding="utf-8"))
    assert payload == {"state": "waiting", "last_sequence": 3}
    assert not (tmp_path / "gpu-status.json.part").exists()


def test_live_worker_switches_profile_from_start_command_without_loading_early(tmp_path: Path) -> None:
    loaded_profiles = []
    worker = LiveWorker(tmp_path, separator_factory=lambda profile: loaded_profiles.append(profile))
    write_command(
        tmp_path,
        "start",
        42,
        monitor_stem="piano",
        profile_name="六轨 · 加吉他/钢琴",
    )

    worker.process_available()

    assert worker.active_profile.name == "六轨 · 加吉他/钢琴"
    assert loaded_profiles == []


def test_live_worker_quarantines_failed_chunk_and_continues_with_next(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    for sequence in (1, 2):
        _write_audio(inbox / f"capture-{sequence:08d}.wav", 80)

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

    assert worker.process_available(max_chunks=1) == []
    assert (tmp_path / "failed" / "capture-00000001.wav").is_file()
    failed_manifest = json.loads(
        (tmp_path / "outbox" / "result-00000001.json").read_text(encoding="utf-8")
    )
    assert failed_manifest["sequence"] == 1
    assert "missing generated vocal stem" in failed_manifest["error"]

    results = worker.process_available(max_chunks=1)
    assert [result.sequence for result in results] == [2]
    assert len(created) == 2
