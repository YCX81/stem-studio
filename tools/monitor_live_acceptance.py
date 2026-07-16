from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = PROJECT_ROOT / "data" / "live" / "acceptance-monitor-report.json"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stemstudio.acceptance import (
    DEFAULT_MIXER_LATENCY_LIMIT_MS,
    LiveAcceptanceRecorder,
)
from stemstudio.live_control import live_pipeline_snapshot


class MonitorAlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        suffix = f" (pid {pid})" if pid is not None else ""
        super().__init__(f"acceptance monitor is already running{suffix}")


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_pid(pid_path: Path, pid: int) -> None:
    pid_path.write_text(f"{pid}\n", encoding="ascii")


def _read_pid(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


@contextmanager
def _exclusive_monitor(pid_path: Path) -> Iterator[None]:
    """Hold an OS-owned lock so a crash cannot leave the monitor blocked."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = pid_path.with_name(f"{pid_path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(descriptor, "r+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        try:
            _lock_file(handle)
        except OSError as exc:
            owner_pid = _read_pid(pid_path)
            raise MonitorAlreadyRunning(owner_pid) from exc

        try:
            _write_pid(pid_path, os.getpid())
            yield
        finally:
            _write_pid(pid_path, 0)
            _unlock_file(handle)
    finally:
        handle.close()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(
        f"{path.name}.monitor-{os.getpid()}-{threading.get_ident()}.part"
    )
    partial.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for attempt in range(50):
        try:
            os.replace(partial, path)
            return
        except PermissionError:
            if attempt == 49:
                partial.unlink(missing_ok=True)
                raise
            time.sleep(0.002)


def monitor(
    *,
    live_root: Path,
    report_path: Path,
    pid_path: Path,
    timeout_seconds: float,
    poll_seconds: float,
    mixer_latency_limit_ms: float,
) -> int:
    if timeout_seconds <= 0.0 or poll_seconds <= 0.0:
        raise ValueError("timeout and poll interval must be positive")
    try:
        with _exclusive_monitor(pid_path):
            return _monitor_exclusive(
                live_root=live_root,
                report_path=report_path,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                mixer_latency_limit_ms=mixer_latency_limit_ms,
            )
    except MonitorAlreadyRunning as exc:
        print(
            json.dumps(
                {"state": "already_running", "pid": exc.pid},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 4


def _monitor_exclusive(
    *,
    live_root: Path,
    report_path: Path,
    timeout_seconds: float,
    poll_seconds: float,
    mixer_latency_limit_ms: float,
) -> int:
    recorder = LiveAcceptanceRecorder(
        mixer_latency_limit_ms=mixer_latency_limit_ms,
    )
    started = time.monotonic()
    last_state = ""
    try:
        while True:
            recorder.observe(live_pipeline_snapshot(live_root))
            report = recorder.report()
            state = str(report["state"])
            _atomic_json(report_path, report)
            if state != last_state:
                print(
                    json.dumps(
                        {
                            "state": state,
                            "requirements": report["requirements"],
                            "metrics": report["metrics"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last_state = state
            if state == "passed":
                return 0
            if state == "failed":
                return 2
            if time.monotonic() - started >= timeout_seconds:
                report["state"] = "timed_out"
                report["passed"] = False
                _atomic_json(report_path, report)
                print(json.dumps(report, ensure_ascii=False), flush=True)
                return 3
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        report = recorder.report()
        report["state"] = "stopped"
        report["passed"] = False
        _atomic_json(report_path, report)
        return 130


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor real-phone first-play, replay, continuity, and mixer acceptance."
    )
    parser.add_argument("--live-root", type=Path, default=PROJECT_ROOT / "data" / "live")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=PROJECT_ROOT / "data" / "live" / "acceptance-monitor.pid",
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument(
        "--mixer-latency-limit-ms",
        type=float,
        default=DEFAULT_MIXER_LATENCY_LIMIT_MS,
    )
    args = parser.parse_args()
    raise SystemExit(
        monitor(
            live_root=args.live_root.resolve(),
            report_path=args.report.resolve(),
            pid_path=args.pid_file.resolve(),
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            mixer_latency_limit_ms=args.mixer_latency_limit_ms,
        )
    )


if __name__ == "__main__":
    main()
