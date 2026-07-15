from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stemstudio.acceptance import LiveAcceptanceRecorder
from stemstudio.live_control import live_pipeline_snapshot


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f"{path.suffix}.part")
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
    recorder = LiveAcceptanceRecorder(
        mixer_latency_limit_ms=mixer_latency_limit_ms,
    )
    started = time.monotonic()
    last_state = ""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
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
    finally:
        try:
            if int(pid_path.read_text(encoding="ascii").strip()) == os.getpid():
                pid_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor real-phone first-play, replay, continuity, and mixer acceptance."
    )
    parser.add_argument("--live-root", type=Path, default=PROJECT_ROOT / "data" / "live")
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "data" / "live" / "acceptance-report.json",
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=PROJECT_ROOT / "data" / "live" / "acceptance-monitor.pid",
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--mixer-latency-limit-ms", type=float, default=100.0)
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
