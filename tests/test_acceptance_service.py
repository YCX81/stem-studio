import json
import time
from pathlib import Path

from stemstudio.acceptance_service import (
    LiveAcceptanceService,
    start_acceptance_service,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_embedded_acceptance_service_keeps_refreshing_report(
    tmp_path: Path,
) -> None:
    thread, stop_event = start_acceptance_service(tmp_path, poll_seconds=0.01)
    report_path = tmp_path / "acceptance-report.json"
    deadline = time.monotonic() + 1.0
    while not report_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert report_path.exists()
    first_mtime = report_path.stat().st_mtime_ns

    deadline = time.monotonic() + 1.0
    while report_path.stat().st_mtime_ns <= first_mtime and time.monotonic() < deadline:
        time.sleep(0.01)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert thread.is_alive()
    assert report["state"] == "waiting_for_phone"
    assert report["service"]["embedded"] is True

    stop_event.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_embedded_acceptance_service_resets_on_new_start_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "outbox").mkdir()
    (tmp_path / "inbox" / "capture-00000001.wav").touch()
    _write(
        tmp_path / "airplay-status.json",
        {"state": "streaming", "enabled": True, "pcm_frames": 44_100},
    )
    _write(
        tmp_path / "playback-status.json",
        {
            "state": "playing",
            "sequence": 1,
            "underruns": 0,
            "mixer_updates": 0,
        },
    )
    _write(
        tmp_path / "gpu-status.json",
        {
            "state": "running",
            "last_sequence": 1,
            "cache_hits": 0,
            "cache_misses": 0,
            "songs_cached": 0,
        },
    )
    _write(
        tmp_path / "command.json",
        {"sequence": 1, "action": "start_airplay"},
    )
    service = LiveAcceptanceService(tmp_path)
    assert service.mixer_latency_limit_ms == 50.0

    first = service.sample(observed_at_ns=1)
    (tmp_path / "inbox" / "capture-00000002.wav").touch()
    _write(
        tmp_path / "outbox" / "result-00000002.json",
        {
            "sequence": 2,
            "cache_hit": True,
            "cache_scope": "song",
            "cache_key": "a" * 64,
        },
    )
    _write(
        tmp_path / "gpu-status.json",
        {
            "state": "running",
            "last_sequence": 2,
            "cache_hits": 1,
            "cache_misses": 1,
            "songs_cached": 1,
        },
    )
    _write(
        tmp_path / "playback-status.json",
        {
            "state": "playing",
            "sequence": 2,
            "underruns": 1,
            "mixer_updates": 1,
            "last_mixer_control_latency_ms": 20.0,
        },
    )
    failed = service.sample(observed_at_ns=2)

    assert first["state"] == "waiting_for_phone"
    assert failed["state"] == "failed"
    assert failed["metrics"]["active_underrun_delta"] == 1

    _write(
        tmp_path / "command.json",
        {"sequence": 2, "action": "start_airplay"},
    )
    reset = service.sample(observed_at_ns=3)

    assert reset["state"] == "waiting_for_phone"
    assert reset["metrics"]["active_underrun_delta"] == 0
    assert reset["service"]["command_sequence"] == 2
