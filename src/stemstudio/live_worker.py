from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .core import LIVE_PROFILES, LiveProfile
from .live import LiveChunkProcessor, LiveConfig, PersistentSeparator, ProcessedChunk, discover_ready_chunks


_result_pattern = re.compile(r"^result-(\d{8})\.json$")


def last_published_sequence(outbox: str | Path) -> int:
    root = Path(outbox)
    if not root.is_dir():
        return 0
    sequences = []
    for path in root.iterdir():
        match = _result_pattern.fullmatch(path.name)
        if match:
            sequences.append(int(match.group(1)))
    return max(sequences, default=0)


class LiveWorker:
    def __init__(
        self,
        data_root: str | Path,
        separator_factory: Callable[[LiveProfile], PersistentSeparator] | None = None,
    ) -> None:
        self.root = Path(data_root).resolve()
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.work = self.root / "work"
        self.failed = self.root / "failed"
        for directory in (self.inbox, self.outbox, self.work, self.failed):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = LiveConfig()
        self._separator_factory = separator_factory or self._default_separator
        self._processor: LiveChunkProcessor | None = None
        self._last_sequence = last_published_sequence(self.outbox)
        self._active_profile = LIVE_PROFILES["人声 / 伴奏 · 高质量"]
        self._profile_command_sequence = 0
        self._last_failure: dict | None = None

    @property
    def active_profile(self) -> LiveProfile:
        return self._active_profile

    def _default_separator(self, profile: LiveProfile) -> PersistentSeparator:
        return PersistentSeparator(
            model_dir=self.root.parent / "models",
            work_dir=self.work,
            model_filename=profile.model_filename,
        )

    def _sync_requested_profile(self) -> None:
        command_path = self.root / "command.json"
        if not command_path.is_file():
            return
        payload = json.loads(command_path.read_text(encoding="utf-8-sig"))
        sequence = int(payload.get("sequence", 0))
        if sequence <= self._profile_command_sequence or payload.get("action") != "start":
            return
        profile_name = str(payload.get("profile_name", "人声 / 伴奏 · 高质量"))
        if profile_name not in LIVE_PROFILES:
            raise ValueError("实时控制命令包含未知分离模式。")
        requested = LIVE_PROFILES[profile_name]
        if requested != self._active_profile:
            self._active_profile = requested
            self._processor = None
        self._profile_command_sequence = sequence

    def _quarantine(self, chunk, error: Exception) -> None:
        message = str(error).strip() or type(error).__name__
        payload = {
            "version": 1,
            "sequence": chunk.sequence,
            "error": message,
            "stems": {},
        }
        manifest = self.outbox / f"result-{chunk.sequence:08d}.json"
        partial = manifest.with_suffix(".json.part")
        partial.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, manifest)
        destination = self.failed / chunk.path.name
        if chunk.path.is_file():
            os.replace(chunk.path, destination)
        prefix = f"capture-{chunk.sequence:08d}_"
        for generated in self.work.glob(f"{prefix}*"):
            if generated.is_file():
                os.replace(generated, self.failed / generated.name)
        self._last_sequence = chunk.sequence
        self._last_failure = {"sequence": chunk.sequence, "error": message}

    def process_available(self, max_chunks: int = 1) -> list[ProcessedChunk]:
        self._sync_requested_profile()
        ready = discover_ready_chunks(self.inbox, self._last_sequence)[:max_chunks]
        if not ready:
            return []
        if self._processor is None:
            self._processor = LiveChunkProcessor(
                self.config,
                self.outbox,
                self._separator_factory(self._active_profile),
                expected_stems=self._active_profile.stems,
            )
        results = []
        for chunk in ready:
            try:
                result = self._processor.process(chunk)
            except Exception as exc:
                self._quarantine(chunk, exc)
                # A separator can retain partial per-file state after an exception.
                # Recreate it for the next window so one bad output cannot poison the session.
                self._processor = None
            else:
                self._last_sequence = result.sequence
                self._last_failure = None
                results.append(result)
        return results

    def run(self, stop_event: threading.Event, poll_seconds: float = 0.25) -> None:
        self._write_status({"state": "waiting", "last_sequence": self._last_sequence})
        while not stop_event.is_set():
            try:
                results = self.process_available()
                if results:
                    latest = results[-1]
                    self._write_status(
                        {
                            "state": "running",
                            "last_sequence": latest.sequence,
                            "latency_seconds": round(latest.latency_seconds, 3),
                            "profile_name": self._active_profile.name,
                        }
                    )
                elif self._last_failure:
                    self._write_status(
                        {
                            "state": "degraded",
                            "last_sequence": self._last_sequence,
                            "failed_sequence": self._last_failure["sequence"],
                            "error": self._last_failure["error"],
                            "recovering": True,
                            "profile_name": self._active_profile.name,
                        }
                    )
            except Exception as exc:
                self._write_status({"state": "error", "error": str(exc), "last_sequence": self._last_sequence})
                time.sleep(1.0)
            stop_event.wait(poll_seconds)

    def _write_status(self, payload: dict) -> None:
        destination = self.root / "gpu-status.json"
        partial = destination.with_suffix(".json.part")
        partial.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, destination)


def start_live_worker(data_root: str | Path) -> tuple[threading.Thread, threading.Event]:
    worker = LiveWorker(data_root)
    stop_event = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop_event,), name="live-gpu-worker", daemon=True)
    thread.start()
    return thread, stop_event
