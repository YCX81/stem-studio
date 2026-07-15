import time
import wave
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from stemstudio.inference_process import (
    InferenceDeadlineExceeded,
    InferenceProcessError,
    InferenceWarmingUp,
    IsolatedPersistentSeparator,
    _warm_up_separator,
)


def _fake_inference_worker(connection: Connection, config: dict) -> None:
    startup_delay = float(config.get("startup_delay", 0.0))
    if startup_delay:
        time.sleep(startup_delay)
    if config.get("startup_error"):
        connection.send({"kind": "startup_error", "error": config["startup_error"]})
        return
    connection.send(
        {"kind": "ready", "warmup_seconds": float(config.get("warmup_seconds", 0.0))}
    )
    while True:
        request = connection.recv()
        if request["kind"] == "shutdown":
            return
        if config.get("hang"):
            time.sleep(10.0)
            continue
        connection.send(
            {
                "kind": "result",
                "request_id": request["request_id"],
                "outputs": config["outputs"],
            }
        )


def _separator(tmp_path: Path, **config) -> IsolatedPersistentSeparator:
    return IsolatedPersistentSeparator(
        model_dir=tmp_path / "models",
        work_dir=tmp_path / "work",
        model_filename="fast.yaml",
        inference_timeout_seconds=0.2,
        child_target=_fake_inference_worker,
        child_config=config,
    )


def test_isolated_separator_prewarms_without_blocking_and_returns_paths(tmp_path: Path) -> None:
    outputs = [str(tmp_path / "vocals.wav"), str(tmp_path / "other.wav")]
    started = time.perf_counter()
    separator = _separator(tmp_path, startup_delay=0.2, outputs=outputs)
    construction_seconds = time.perf_counter() - started
    source = tmp_path / "capture.wav"
    source.write_bytes(b"pcm")
    try:
        assert construction_seconds < 0.15
        assert separator.wait_until_ready(2.0) is True
        assert separator.separate(source) == [Path(path) for path in outputs]
    finally:
        separator.close()
    assert separator.is_alive is False


def test_cuda_prewarm_runs_a_full_audio_window_and_removes_its_artifacts(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    observed: dict[str, int] = {}

    class InspectingSeparator:
        def separate(self, source: Path) -> list[Path]:
            with wave.open(str(source), "rb") as audio:
                observed.update(
                    sample_rate=audio.getframerate(),
                    channels=audio.getnchannels(),
                    frames=audio.getnframes(),
                )
                pcm = audio.readframes(audio.getnframes())
            assert any(pcm)
            output = work / f"{source.stem}_(Vocals).wav"
            output.write_bytes(b"generated")
            return [output]

    elapsed = _warm_up_separator(
        InspectingSeparator(),
        work,
        sample_rate=10,
        channels=2,
        window_seconds=12,
    )

    assert elapsed >= 0.0
    assert observed == {"sample_rate": 10, "channels": 2, "frames": 120}
    assert list(work.iterdir()) == []


def test_isolated_separator_reports_real_model_warmup_state_and_duration(tmp_path: Path) -> None:
    separator = _separator(
        tmp_path,
        startup_delay=0.2,
        warmup_seconds=1.75,
        outputs=[],
    )
    try:
        warming = separator.status_snapshot()
        assert warming["model_state"] == "warming"
        assert warming["inference_process_pid"] == separator.process_id
        assert warming["inference_timeout_seconds"] == 0.2

        assert separator.wait_until_ready(2.0) is True
        ready = separator.status_snapshot()
        assert ready["model_state"] == "ready"
        assert ready["model_warmup_seconds"] == 1.75
    finally:
        separator.close()

    assert separator.status_snapshot()["model_state"] == "stopped"


def test_isolated_separator_reports_warmup_without_consuming_realtime_deadline(
    tmp_path: Path,
) -> None:
    separator = _separator(tmp_path, startup_delay=0.5, outputs=[])
    try:
        started = time.perf_counter()
        with pytest.raises(InferenceWarmingUp, match="预热"):
            separator.separate(tmp_path / "capture.wav")
        assert time.perf_counter() - started < 0.15
        assert separator.is_alive is True
        assert separator.wait_until_ready(2.0) is True
    finally:
        separator.close()


def test_isolated_separator_kills_hung_gpu_process_at_deadline(tmp_path: Path) -> None:
    separator = _separator(tmp_path, hang=True, outputs=[])
    source = tmp_path / "capture.wav"
    source.write_bytes(b"pcm")
    assert separator.wait_until_ready(2.0) is True

    started = time.perf_counter()
    with pytest.raises(InferenceDeadlineExceeded, match="0.2"):
        separator.separate(source)

    assert time.perf_counter() - started < 1.0
    assert separator.is_alive is False


def test_isolated_separator_surfaces_model_startup_failure(tmp_path: Path) -> None:
    separator = _separator(tmp_path, startup_error="CUDA model load failed")
    try:
        with pytest.raises(InferenceProcessError, match="CUDA model load failed"):
            separator.wait_until_ready(2.0)
    finally:
        separator.close()
