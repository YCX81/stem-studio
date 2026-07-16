from pathlib import Path
import sys
from types import SimpleNamespace

from stemstudio.core import SeparationRequest
from stemstudio.engine import AudioSeparatorEngine, gpu_diagnostics


class FakeSeparator:
    init_kwargs = None
    loaded_model = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    def load_model(self, model_filename: str) -> None:
        type(self).loaded_model = model_filename

    def separate(self, source: str):
        output_dir = Path(type(self).init_kwargs["output_dir"])
        result = output_dir / "song_(Vocals).flac"
        result.write_bytes(b"stem")
        return [result.name]


def test_engine_configures_cache_gpu_autocast_and_returns_files(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")
    request = SeparationRequest.create(
        source, "人声 / 伴奏 · 高质量", "FLAC", tmp_path / "outputs"
    )
    engine = AudioSeparatorEngine(
        model_dir=tmp_path / "models",
        separator_factory=FakeSeparator,
        mdxc_segment_size=384,
    )

    results = engine.separate(request)

    assert FakeSeparator.init_kwargs["model_file_dir"] == str((tmp_path / "models").resolve())
    assert FakeSeparator.init_kwargs["output_dir"] == str(request.output_dir)
    assert FakeSeparator.init_kwargs["output_format"] == "FLAC"
    assert FakeSeparator.init_kwargs["use_autocast"] is True
    assert FakeSeparator.init_kwargs["mdxc_params"]["batch_size"] == 1
    assert FakeSeparator.init_kwargs["mdxc_params"]["segment_size"] == 384
    assert FakeSeparator.init_kwargs["demucs_params"]["segment_size"] == "Default"
    assert FakeSeparator.loaded_model == request.model_filename
    assert len(results) == 1 and results[0].exists()


def test_gpu_diagnostics_reports_cuda_device(monkeypatch) -> None:
    properties = SimpleNamespace(total_memory=8 * 1024**3)
    fake_cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_name=lambda _index: "NVIDIA Test GPU",
        get_device_capability=lambda _index: (12, 0),
        get_device_properties=lambda _index: properties,
    )
    fake_torch = SimpleNamespace(
        __version__="2.12.0",
        version=SimpleNamespace(cuda="13.0"),
        cuda=fake_cuda,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    result = gpu_diagnostics()

    assert result == {
        "available": True,
        "torch": "2.12.0",
        "cuda": "13.0",
        "device": "NVIDIA Test GPU",
        "capability": "12.0",
        "vram": "8.0 GB",
    }


def test_gpu_diagnostics_reports_cpu_only(monkeypatch) -> None:
    fake_torch = SimpleNamespace(
        __version__="2.12.0",
        version=SimpleNamespace(cuda=None),
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    result = gpu_diagnostics()

    assert result == {"available": False, "torch": "2.12.0", "cuda": "不可用"}


def test_gpu_diagnostics_never_raises(monkeypatch) -> None:
    fake_torch = SimpleNamespace(
        __version__="broken",
        version=SimpleNamespace(cuda=None),
        cuda=SimpleNamespace(is_available=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    result = gpu_diagnostics()

    assert result == {"available": False, "error": "boom"}
