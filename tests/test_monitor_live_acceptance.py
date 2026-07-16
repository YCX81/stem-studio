import json
import os
import threading
from pathlib import Path

import pytest

import tools.monitor_live_acceptance as monitor_module


def test_external_monitor_uses_a_report_separate_from_the_embedded_service() -> None:
    embedded_report = monitor_module.PROJECT_ROOT / "data" / "live" / "acceptance-report.json"

    assert monitor_module.DEFAULT_REPORT_PATH.name == "acceptance-monitor-report.json"
    assert monitor_module.DEFAULT_REPORT_PATH != embedded_report
    assert monitor_module.DEFAULT_MIXER_LATENCY_LIMIT_MS == 50.0


def test_atomic_json_supports_concurrent_monitor_writers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "acceptance-monitor-report.json"
    real_replace = os.replace
    both_writers_ready = threading.Barrier(2)
    replace_lock = threading.Lock()
    errors: list[BaseException] = []
    partial_paths: list[Path] = []

    def synchronized_replace(source: str | bytes | os.PathLike, destination: str | bytes | os.PathLike) -> None:
        source_path = Path(source)
        partial_paths.append(source_path)
        both_writers_ready.wait(timeout=1.0)
        with replace_lock:
            if source_path.exists():
                real_replace(source, destination)

    monkeypatch.setattr(monitor_module.os, "replace", synchronized_replace)

    def write_report(writer: int) -> None:
        try:
            monitor_module._atomic_json(report_path, {"writer": writer})
        except BaseException as exc:  # Preserve the exact cross-thread failure for assertion.
            errors.append(exc)

    writers = [
        threading.Thread(target=write_report, args=(writer,), daemon=True)
        for writer in (1, 2)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=2.0)

    assert all(not writer.is_alive() for writer in writers)
    assert errors == []
    assert len(set(partial_paths)) == 2
    assert json.loads(report_path.read_text(encoding="utf-8"))["writer"] in {1, 2}
    assert list(tmp_path.glob("*.part")) == []


def test_monitor_pid_lock_rejects_a_second_owner_and_can_be_reused(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "acceptance-monitor.pid"

    with monitor_module._exclusive_monitor(pid_path):
        assert int(pid_path.read_text(encoding="ascii").strip()) == os.getpid()
        with pytest.raises(monitor_module.MonitorAlreadyRunning) as conflict:
            with monitor_module._exclusive_monitor(pid_path):
                pass

        assert conflict.value.pid == os.getpid()

    assert pid_path.read_text(encoding="ascii").strip() == "0"

    with monitor_module._exclusive_monitor(pid_path):
        assert int(pid_path.read_text(encoding="ascii").strip()) == os.getpid()


def test_monitor_reports_existing_owner_without_sampling_live_state(
    tmp_path: Path,
    capsys,
) -> None:
    pid_path = tmp_path / "acceptance-monitor.pid"

    with monitor_module._exclusive_monitor(pid_path):
        exit_code = monitor_module.monitor(
            live_root=tmp_path,
            report_path=tmp_path / "report.json",
            pid_path=pid_path,
            timeout_seconds=1.0,
            poll_seconds=0.1,
            mixer_latency_limit_ms=50.0,
        )

    assert exit_code == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "already_running",
        "pid": os.getpid(),
    }
