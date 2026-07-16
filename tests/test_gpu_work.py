import pytest

from stemstudio.gpu_work import GpuWorkCoordinator


def test_file_capacity_uses_detected_concurrency_and_blocks_live() -> None:
    coordinator = GpuWorkCoordinator(file_capacity=2)
    first = coordinator.reserve_file()
    second = coordinator.reserve_file()
    with pytest.raises(RuntimeError, match="文件分离"):
        coordinator.reserve_live()
    with pytest.raises(RuntimeError, match="并发上限"):
        coordinator.reserve_file()
    second.release()
    first.release()
    live = coordinator.reserve_live()
    with pytest.raises(RuntimeError, match="实时分离"):
        coordinator.reserve_file()
    live.release()


def test_reservation_release_is_idempotent() -> None:
    coordinator = GpuWorkCoordinator(file_capacity=1)
    reservation = coordinator.reserve_file()
    reservation.release()
    reservation.release()
    coordinator.reserve_file().release()
