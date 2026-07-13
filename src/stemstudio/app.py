from __future__ import annotations

import os
from pathlib import Path

import gradio as gr

from .core import LIVE_PROFILES, MODEL_PROFILES, SeparationRequest
from .engine import AudioSeparatorEngine, gpu_diagnostics
from .live_worker import start_live_worker
from .live_control import (
    all_monitor_choices,
    monitor_choices,
    read_processes,
    routing_markdown,
    status_markdown,
    write_command,
)


DATA_ROOT = Path(os.environ.get("STEM_STUDIO_DATA", "/data")).resolve()
MODEL_DIR = DATA_ROOT / "models"
OUTPUT_DIR = DATA_ROOT / "outputs"
LIVE_DIR = DATA_ROOT / "live"

CSS = """
.gradio-container { max-width: 1120px !important; }
.hero { padding: 28px 6px 12px; }
.hero h1 { font-size: 2.1rem; margin-bottom: 6px; }
.hero p { color: #6b7280; font-size: 1.04rem; }
.status-card { border-radius: 14px; }
"""


def _gpu_status_markdown() -> str:
    info = gpu_diagnostics()
    if not info.get("available"):
        return f"🔴 **GPU 未就绪** · {info.get('error', '容器未检测到 CUDA')}"
    return (
        f"🟢 **GPU 已就绪** · {info['device']} · {info['vram']} · "
        f"CUDA {info['cuda']} · PyTorch {info['torch']} · 算力 {info['capability']}"
    )


def run_separation(
    source: str | None,
    profile_name: str,
    output_format: str,
) -> tuple[str, list[str]]:
    if not source:
        return "请先拖入或选择一个音频文件。", []
    try:
        request = SeparationRequest.create(
            source=source,
            profile_name=profile_name,
            output_format=output_format,
            output_root=OUTPUT_DIR,
        )
        outputs = AudioSeparatorEngine(MODEL_DIR).separate(request)
        message = (
            f"✅ 分离完成：生成 {len(outputs)} 条音轨。\n\n"
            f"保存目录：`{request.output_dir}`"
        )
        return message, [str(path) for path in outputs]
    except Exception as exc:
        return f"❌ 分离失败：{exc}", []


def refresh_live_processes():
    choices = read_processes(LIVE_DIR)
    return gr.Dropdown(choices=choices, value=choices[0][1] if choices else None)


def refresh_monitor_stems(profile_name: str):
    choices = monitor_choices(profile_name)
    return gr.Radio(choices=choices, value=choices[0][1])


def start_live_capture(process_id: int | None, profile_name: str, monitor_stem: str) -> str:
    try:
        write_command(
            LIVE_DIR,
            "start",
            process_id,
            monitor_stem=monitor_stem,
            profile_name=profile_name,
        )
        latency = {
            "人声 / 伴奏 · 高质量": "约 25–30 秒",
            "四轨 · 人声/鼓/贝斯/其他": "约 22–26 秒",
            "六轨 · 加吉他/钢琴": "约 16–20 秒",
        }[profile_name]
        return f"🟡 **正在启动 {profile_name}** · 首个 AI 结果{latency}"
    except Exception as exc:
        return f"🔴 **启动失败** · {exc}"


def stop_live_capture() -> str:
    write_command(LIVE_DIR, "stop")
    return "⚪ **已发送停止命令**"


def open_audio_settings() -> str:
    write_command(LIVE_DIR, "open_audio_settings")
    return (
        "🟡 **已请求打开 Windows 音量混合器** · "
        "将目标音乐软件的输出设备改为虚拟音频设备，然后返回刷新检测。"
    )


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Stem Studio") as app:
        gr.HTML(
            "<div class='hero'><h1>Stem Studio</h1>"
            "<p>本地 GPU 音频分离 · 文件不上传 · 模型自动缓存</p></div>"
        )
        gr.Markdown(_gpu_status_markdown(), elem_classes=["status-card"])
        with gr.Tabs():
            with gr.Tab("实时分离"):
                gr.Markdown(
                    "自动捕获所选 Windows 音乐软件及其子进程。"
                    "可实时切换二轨、四轨或六轨模型，均按 12 秒连续窗口工作。"
                )
                with gr.Row():
                    live_process = gr.Dropdown(label="正在运行的音乐软件", choices=[])
                    refresh_processes = gr.Button("刷新软件列表")
                live_profile = gr.Dropdown(
                    choices=list(LIVE_PROFILES),
                    value="人声 / 伴奏 · 高质量",
                    label="实时分离模式",
                )
                monitor_stem = gr.Radio(
                    choices=all_monitor_choices(),
                    value="instrumental",
                    label="监听音轨",
                )
                with gr.Row():
                    start_live = gr.Button("开始实时捕获", variant="primary")
                    stop_live = gr.Button("停止", variant="stop")
                    refresh_status = gr.Button("刷新状态")
                live_status = gr.Markdown(status_markdown(LIVE_DIR))
                routing_status = gr.Markdown(routing_markdown(LIVE_DIR))
                with gr.Row():
                    open_routing = gr.Button("打开 Windows 音量混合器")
                    refresh_routing = gr.Button("刷新纯净监听检测")
                refresh_processes.click(refresh_live_processes, outputs=live_process)
                live_profile.change(refresh_monitor_stems, inputs=live_profile, outputs=monitor_stem)
                app.load(refresh_monitor_stems, inputs=live_profile, outputs=monitor_stem)
                start_live.click(
                    start_live_capture,
                    inputs=[live_process, live_profile, monitor_stem],
                    outputs=live_status,
                )
                stop_live.click(stop_live_capture, outputs=live_status)
                refresh_status.click(lambda: status_markdown(LIVE_DIR), outputs=live_status)
                open_routing.click(open_audio_settings, outputs=routing_status)
                refresh_routing.click(lambda: routing_markdown(LIVE_DIR), outputs=routing_status)
            with gr.Tab("文件分离"):
                with gr.Row():
                    with gr.Column(scale=5):
                        source = gr.File(
                            label="音频文件",
                            file_types=["audio"],
                            type="filepath",
                        )
                        profile = gr.Dropdown(
                            choices=list(MODEL_PROFILES),
                            value="人声 / 伴奏 · 高质量",
                            label="分离模式",
                        )
                        output_format = gr.Radio(
                            choices=["FLAC", "WAV", "MP3"],
                            value="FLAC",
                            label="输出格式",
                        )
                        run_button = gr.Button("开始 GPU 分离", variant="primary", size="lg")
                    with gr.Column(scale=5):
                        status = gr.Markdown("等待任务。")
                        outputs = gr.File(label="分离结果", file_count="multiple")
        gr.Markdown(
            "首次使用某个模式会下载模型，之后会从本地缓存直接加载。"
            "8GB 显存一次只运行一个任务，避免并发导致显存不足。"
        )
        run_button.click(
            fn=run_separation,
            inputs=[source, profile, output_format],
            outputs=[status, outputs],
            concurrency_limit=1,
        )
    return app


def main() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start_live_worker(DATA_ROOT / "live")
    app = build_app()
    app.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        css=CSS,
        theme=gr.themes.Soft(),
        allowed_paths=[str(OUTPUT_DIR)],
        show_error=True,
    )


if __name__ == "__main__":
    main()
