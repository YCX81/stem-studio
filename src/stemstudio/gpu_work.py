from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass
class GpuReservation:
    _release_callback: Callable[[], None]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release_callback()


class GpuWorkCoordinator:
    """Prevent live inference and batch file work from competing for VRAM."""

    def __init__(self, file_capacity: int) -> None:
        if file_capacity < 1:
            raise ValueError("GPU 文件并发上限必须为正数。")
        self.file_capacity = file_capacity
        self._lock = threading.Lock()
        self._file_tasks = 0
        self._live_reserved = False

    def reserve_file(self) -> GpuReservation:
        with self._lock:
            if self._live_reserved:
                raise RuntimeError("实时分离正在使用 GPU，请先停止实时捕获。")
            if self._file_tasks >= self.file_capacity:
                raise RuntimeError("GPU 文件任务已达到当前硬件并发上限。")
            self._file_tasks += 1
        return GpuReservation(self._release_file)

    def reserve_live(self) -> GpuReservation:
        with self._lock:
            if self._live_reserved:
                raise RuntimeError("实时分离已经启动。")
            if self._file_tasks:
                raise RuntimeError("文件分离仍在运行，请等待任务完成后再启动实时捕获。")
            self._live_reserved = True
        return GpuReservation(self._release_live)

    def _release_file(self) -> None:
        with self._lock:
            self._file_tasks = max(0, self._file_tasks - 1)

    def _release_live(self) -> None:
        with self._lock:
            self._live_reserved = False
