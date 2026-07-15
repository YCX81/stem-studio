from __future__ import annotations

import multiprocessing
import struct
import time
import uuid
import wave
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any


class InferenceProcessError(RuntimeError):
    pass


class InferenceWarmingUp(InferenceProcessError):
    pass


class InferenceDeadlineExceeded(InferenceProcessError):
    pass


def _error_text(error: BaseException) -> str:
    return (str(error).strip() or type(error).__name__)[:2_048]


def _warm_up_separator(
    separator: Any,
    work_dir: str | Path,
    *,
    sample_rate: int = 44_100,
    channels: int = 2,
    window_seconds: int = 12,
) -> float:
    """Exercise the complete model graph before declaring it realtime-ready."""
    if sample_rate <= 0 or channels <= 0 or window_seconds <= 0:
        raise ValueError("实时模型预热音频参数必须为正数。")
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    token = f"stemstudio-prewarm-{uuid.uuid4().hex}"
    source = work / f"{token}.wav"
    frame_values = tuple(64 if index % 2 == 0 else -64 for index in range(channels))
    frame = struct.pack(f"<{channels}h", *frame_values)
    total_frames = sample_rate * window_seconds
    block_frames = min(sample_rate, total_frames)
    started = time.perf_counter()
    try:
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            remaining = total_frames
            while remaining:
                count = min(block_frames, remaining)
                audio.writeframes(frame * count)
                remaining -= count
        separator.separate(source)
        return time.perf_counter() - started
    finally:
        # The token is random and scoped to this warmup, so this cannot remove
        # a user's capture or a live result even when separation fails midway.
        for candidate in work.iterdir():
            if candidate.is_file() and token in candidate.name:
                candidate.unlink(missing_ok=True)


def _persistent_separator_worker(connection: Connection, config: dict) -> None:
    separator = None
    try:
        from .live import PersistentSeparator

        separator = PersistentSeparator(
            model_dir=config["model_dir"],
            work_dir=config["work_dir"],
            model_filename=config["model_filename"],
        )
        warmup_seconds = _warm_up_separator(
            separator,
            config["work_dir"],
            sample_rate=int(config.get("sample_rate", 44_100)),
            channels=int(config.get("channels", 2)),
            window_seconds=int(config.get("window_seconds", 12)),
        )
        connection.send({"kind": "ready", "warmup_seconds": warmup_seconds})
        while True:
            request = connection.recv()
            kind = request.get("kind")
            if kind == "shutdown":
                return
            if kind != "separate":
                raise RuntimeError("推理子进程收到未知请求。")
            request_id = int(request["request_id"])
            try:
                outputs = separator.separate(Path(request["source"]))
                connection.send(
                    {
                        "kind": "result",
                        "request_id": request_id,
                        "outputs": [str(path) for path in outputs],
                    }
                )
            except BaseException as error:
                connection.send(
                    {
                        "kind": "inference_error",
                        "request_id": request_id,
                        "error": _error_text(error),
                    }
                )
    except EOFError:
        return
    except BaseException as error:
        try:
            connection.send({"kind": "startup_error", "error": _error_text(error)})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if separator is not None:
            close = getattr(separator, "close", None)
            if callable(close):
                close()
        connection.close()


class IsolatedPersistentSeparator:
    """Keep CUDA inference resident outside the UI process with a hard deadline."""

    def __init__(
        self,
        model_dir: str | Path,
        work_dir: str | Path,
        model_filename: str,
        inference_timeout_seconds: float,
        *,
        child_target: Callable[[Connection, dict], None] = _persistent_separator_worker,
        child_config: dict[str, Any] | None = None,
        process_context: Any | None = None,
    ) -> None:
        if inference_timeout_seconds <= 0.0:
            raise ValueError("实时推理硬时限必须为正数。")
        if not model_filename.strip():
            raise ValueError("实时推理模型不能为空。")
        self.model_filename = model_filename
        self.inference_timeout_seconds = float(inference_timeout_seconds)
        self._ready = False
        self._closed = False
        self._request_id = 0
        self._model_warmup_seconds: float | None = None
        self._inference_error: str | None = None
        context = process_context or multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        config = (
            {
                "model_dir": str(Path(model_dir).resolve()),
                "work_dir": str(Path(work_dir).resolve()),
                "model_filename": model_filename,
                "sample_rate": 44_100,
                "channels": 2,
                "window_seconds": 12,
            }
            if child_config is None
            else child_config
        )
        self._connection = parent_connection
        self._process = context.Process(
            target=child_target,
            args=(child_connection, config),
            name=f"stem-inference-{Path(model_filename).stem}",
            daemon=True,
        )
        try:
            self._process.start()
        except BaseException:
            parent_connection.close()
            child_connection.close()
            self._closed = True
            raise
        finally:
            child_connection.close()

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._process.is_alive()

    @property
    def process_id(self) -> int | None:
        return self._process.pid

    def status_snapshot(self) -> dict[str, object]:
        if not self._closed and not self._ready and self._inference_error is None:
            try:
                self.wait_until_ready(0.0)
            except InferenceProcessError as error:
                self._inference_error = _error_text(error)
        if self._inference_error is not None:
            state = "error"
        elif self._closed:
            state = "stopped"
        elif self._ready:
            state = "ready"
        else:
            state = "warming"
        return {
            "model_state": state,
            "inference_process_pid": self.process_id,
            "inference_timeout_seconds": self.inference_timeout_seconds,
            "model_warmup_seconds": self._model_warmup_seconds,
            "inference_error": self._inference_error,
        }

    def wait_until_ready(self, timeout_seconds: float = 0.0) -> bool:
        if timeout_seconds < 0.0:
            raise ValueError("模型预热等待时间不能为负数。")
        if self._ready:
            return True
        if self._closed:
            raise InferenceProcessError("实时推理子进程已经关闭。")
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if self._connection.poll(remaining):
                message = self._receive()
                kind = message.get("kind")
                if kind == "ready":
                    self._ready = True
                    try:
                        self._model_warmup_seconds = max(
                            0.0,
                            float(message.get("warmup_seconds", 0.0)),
                        )
                    except (TypeError, ValueError):
                        self._model_warmup_seconds = 0.0
                    return True
                if kind == "startup_error":
                    self._inference_error = (
                        f"实时模型预热失败：{message.get('error', '未知错误')}"
                    )
                    raise InferenceProcessError(self._inference_error)
                raise InferenceProcessError("实时推理子进程在预热阶段返回了无效消息。")
            if not self._process.is_alive():
                self._inference_error = (
                    f"实时模型子进程在预热阶段退出，退出码 {self._process.exitcode}。"
                )
                raise InferenceProcessError(self._inference_error)
            if time.monotonic() >= deadline:
                return False

    def separate(self, source: Path) -> list[Path]:
        if not self.wait_until_ready(0.0):
            raise InferenceWarmingUp("实时模型仍在后台预热，本窗使用原声保底。")
        if not source.is_file():
            raise FileNotFoundError(source)
        self._request_id += 1
        request_id = self._request_id
        try:
            self._connection.send(
                {
                    "kind": "separate",
                    "request_id": request_id,
                    "source": str(source.resolve()),
                }
            )
        except (BrokenPipeError, EOFError, OSError) as error:
            self.close()
            raise InferenceProcessError("实时推理子进程连接已经断开。") from error

        if not self._connection.poll(self.inference_timeout_seconds):
            timeout = self.inference_timeout_seconds
            self._inference_error = (
                f"GPU 推理超过 {timeout:g} 秒硬时限，已终止子进程并切换原声保底。"
            )
            self.close()
            raise InferenceDeadlineExceeded(self._inference_error)
        message = self._receive()
        if int(message.get("request_id", -1)) != request_id:
            self.close()
            raise InferenceProcessError("实时推理响应序号不一致。")
        if message.get("kind") == "inference_error":
            self._inference_error = (
                f"实时 GPU 推理失败：{message.get('error', '未知错误')}"
            )
            raise InferenceProcessError(self._inference_error)
        if message.get("kind") != "result":
            self.close()
            raise InferenceProcessError("实时推理子进程返回了无效消息。")
        return [Path(path) for path in message.get("outputs", [])]

    def _receive(self) -> dict:
        try:
            message = self._connection.recv()
        except (EOFError, OSError) as error:
            self.close()
            raise InferenceProcessError("实时推理子进程意外退出。") from error
        if not isinstance(message, dict):
            self.close()
            raise InferenceProcessError("实时推理子进程返回了无效数据。")
        return message

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            try:
                self._connection.send({"kind": "shutdown"})
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=0.25)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=0.5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=0.5)
        self._connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass
