from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .acceptance import DEFAULT_MIXER_LATENCY_LIMIT_MS, LiveAcceptanceRecorder
from .live_control import (
    _read_status,
    _replace_with_sharing_retry,
    live_pipeline_snapshot,
)


class LiveAcceptanceService:
    """Keep real-device acceptance evidence inside the desktop app lifetime."""

    def __init__(
        self,
        live_root: str | Path,
        *,
        mixer_latency_limit_ms: float = DEFAULT_MIXER_LATENCY_LIMIT_MS,
    ) -> None:
        self.live_root = Path(live_root)
        self.report_path = self.live_root / "acceptance-report.json"
        self.status_path = self.live_root / "acceptance-service-status.json"
        self.mixer_latency_limit_ms = float(mixer_latency_limit_ms)
        self._recorder = self._new_recorder()
        self._command_sequence = 0
        self._sample_count = 0

    def _new_recorder(self) -> LiveAcceptanceRecorder:
        return LiveAcceptanceRecorder(
            mixer_latency_limit_ms=self.mixer_latency_limit_ms,
        )

    @staticmethod
    def _sequence(payload: dict) -> int:
        try:
            return max(0, int(payload.get("sequence", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _sync_session(self) -> None:
        command = _read_status(self.live_root / "command.json")
        sequence = self._sequence(command)
        if sequence == self._command_sequence:
            return
        self._command_sequence = sequence
        if command.get("action") in {"start", "start_airplay", "stop"}:
            self._recorder = self._new_recorder()

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object], owner: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.{owner}.part")
        partial.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _replace_with_sharing_retry(partial, path)

    def sample(self, *, observed_at_ns: int | None = None) -> dict[str, object]:
        self._sync_session()
        self._recorder.observe(
            live_pipeline_snapshot(self.live_root),
            observed_at_ns=observed_at_ns,
        )
        report = self._recorder.report(observed_at_ns=observed_at_ns)
        self._sample_count += 1
        report["service"] = {
            "embedded": True,
            "pid": os.getpid(),
            "command_sequence": self._command_sequence,
            "samples_written": self._sample_count,
        }
        self._write_json(self.report_path, report, "embedded")
        return report

    def run(self, stop_event: threading.Event, *, poll_seconds: float) -> None:
        if poll_seconds <= 0.0:
            raise ValueError("验收轮询间隔必须为正数。")
        while not stop_event.is_set():
            try:
                self.sample()
                status: dict[str, object] = {
                    "state": "running",
                    "pid": os.getpid(),
                    "command_sequence": self._command_sequence,
                    "samples_written": self._sample_count,
                    "updated_at_ns": time.time_ns(),
                }
            except Exception as exc:
                status = {
                    "state": "degraded",
                    "pid": os.getpid(),
                    "error": str(exc).strip() or type(exc).__name__,
                    "updated_at_ns": time.time_ns(),
                }
            try:
                self._write_json(self.status_path, status, "embedded")
            except OSError:
                pass
            stop_event.wait(poll_seconds)


def start_acceptance_service(
    live_root: str | Path,
    *,
    poll_seconds: float = 0.25,
) -> tuple[threading.Thread, threading.Event]:
    service = LiveAcceptanceService(live_root)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=service.run,
        args=(stop_event,),
        kwargs={"poll_seconds": poll_seconds},
        name="live-acceptance-service",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
