from __future__ import annotations

import os
from pathlib import Path

import gradio as gr

from .acceptance_service import start_acceptance_service
from .core import LIVE_PROFILES, MODEL_PROFILES, SeparationRequest
from .engine import AudioSeparatorEngine, gpu_diagnostics
from .live_worker import start_live_worker
from .lyrics import start_lyrics_service
from .live_control import (
    STEM_LABELS,
    active_stem_visibility,
    live_dashboard_html,
    live_ui_defaults,
    read_processes,
    routing_markdown,
    status_markdown,
    write_command,
    write_mixer_percentages,
)


DATA_ROOT = Path(os.environ.get("STEM_STUDIO_DATA", "/data")).resolve()
MODEL_DIR = DATA_ROOT / "models"
OUTPUT_DIR = DATA_ROOT / "outputs"
LIVE_DIR = DATA_ROOT / "live"
LYRICS_DIR = DATA_ROOT / "lyrics"

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


STEM_ORDER = tuple(STEM_LABELS)


def _mixer_percentages(
    vocals: float,
    instrumental: float,
    drums: float,
    bass: float,
    guitar: float,
    piano: float,
    other: float,
) -> dict[str, float]:
    return dict(
        zip(
            STEM_ORDER,
            (vocals, instrumental, drums, bass, guitar, piano, other),
            strict=True,
        )
    )


def update_live_mix(
    profile_name: str,
    vocals: float,
    instrumental: float,
    drums: float,
    bass: float,
    guitar: float,
    piano: float,
    other: float,
) -> str:
    try:
        sequence = write_mixer_percentages(
            LIVE_DIR,
            profile_name,
            _mixer_percentages(
                vocals,
                instrumental,
                drums,
                bass,
                guitar,
                piano,
                other,
            ),
        )
        return f"🟢 **混音已更新** · 控制序列 {sequence}"
    except Exception as exc:
        return f"🔴 **混音更新失败** · {exc}"


def refresh_mixer_sliders(
    profile_name: str,
    vocals: float,
    instrumental: float,
    drums: float,
    bass: float,
    guitar: float,
    piano: float,
    other: float,
):
    values = _mixer_percentages(
        vocals,
        instrumental,
        drums,
        bass,
        guitar,
        piano,
        other,
    )
    visibility = active_stem_visibility(profile_name)
    status = update_live_mix(
        profile_name,
        vocals,
        instrumental,
        drums,
        bass,
        guitar,
        piano,
        other,
    )
    return tuple(
        gr.Slider(value=values[stem], visible=visibility[stem]) for stem in STEM_ORDER
    ) + (status,)


def input_source_changed(input_source: str):
    return gr.Dropdown(visible=input_source == "process")


def restore_live_controls():
    restored = live_ui_defaults(LIVE_DIR)
    input_source = restored["input_source"]
    profile_name = restored["profile_name"]
    device_endpoint = restored["device_endpoint"]
    gains = restored["gains"]
    visibility = active_stem_visibility(profile_name)
    process_choices = read_processes(LIVE_DIR) if input_source == "process" else []
    process_value = process_choices[0][1] if process_choices else None
    return (
        gr.Radio(value=input_source),
        gr.Dropdown(
            choices=process_choices,
            value=process_value,
            visible=input_source == "process",
        ),
        gr.Dropdown(value=profile_name),
        gr.Textbox(value=device_endpoint),
        *(
            gr.Slider(
                value=round(gains[stem] * 100),
                visible=visibility[stem],
            )
            for stem in STEM_ORDER
        ),
        "🟢 **已恢复当前混音器状态**",
    )


def refresh_live_dashboard() -> tuple[str, str]:
    return status_markdown(LIVE_DIR), live_dashboard_html(LIVE_DIR)


def start_live_capture(
    input_source: str,
    process_id: int | None,
    profile_name: str,
    device_endpoint: str,
    vocals: float,
    instrumental: float,
    drums: float,
    bass: float,
    guitar: float,
    piano: float,
    other: float,
) -> str:
    try:
        write_mixer_percentages(
            LIVE_DIR,
            profile_name,
            _mixer_percentages(
                vocals,
                instrumental,
                drums,
                bass,
                guitar,
                piano,
                other,
            ),
        )
        write_command(
            LIVE_DIR,
            "start_airplay" if input_source == "airplay" else "start",
            None if input_source == "airplay" else process_id,
            monitor_stem=LIVE_PROFILES[profile_name].stems[0],
            profile_name=profile_name,
            device_endpoint=device_endpoint,
        )
        latency = {
            "人声 / 伴奏 · 高质量": "约 25–30 秒",
            "四轨 · 人声/鼓/贝斯/其他": "约 22–26 秒",
            "六轨 · 加吉他/钢琴": "约 16–20 秒",
        }[profile_name]
        if input_source == "airplay":
            return (
                "🟡 **正在启动内置 AirPlay 接收器** · 在手机控制中心选择 Stem Studio · "
                f"首个 AI 结果{latency}"
            )
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
    initial_live = live_ui_defaults(LIVE_DIR)
    initial_source = initial_live["input_source"]
    initial_profile = initial_live["profile_name"]
    initial_device_endpoint = initial_live["device_endpoint"]
    initial_gains = initial_live["gains"]
    with gr.Blocks(title="Stem Studio") as app:
        gr.HTML(
            "<div class='hero'><h1>Stem Studio</h1>"
            "<p>本地 GPU 音频分离 · 文件不上传 · 模型自动缓存</p></div>"
        )
        gr.Markdown(_gpu_status_markdown(), elem_classes=["status-card"])
        with gr.Tabs():
            with gr.Tab("实时分离"):
                gr.Markdown(
                    "可捕获所选 Windows 音乐软件，也可由内置 UxPlay 1.74 宿主接收手机 AirPlay，解码 PCM 后直接分离。"
                    "支持二轨、四轨或六轨实时输出模式；采用 12 秒分析窗、6 秒步进和 100ms 同时间轴交叉拼接。"
                )
                input_source = gr.Radio(
                    choices=[
                        ("Windows 音乐软件", "process"),
                        ("手机 AirPlay → 内置接收器", "airplay"),
                    ],
                    value=initial_source,
                    label="音频来源",
                )
                with gr.Row():
                    live_process = gr.Dropdown(
                        label="正在运行的音乐软件",
                        choices=[],
                        visible=initial_source == "process",
                    )
                    refresh_processes = gr.Button("刷新软件列表")
                live_profile = gr.Dropdown(
                    choices=[
                        (profile.display_name, profile_name)
                        for profile_name, profile in LIVE_PROFILES.items()
                    ],
                    value=initial_profile,
                    label="实时分离模式",
                )
                device_endpoint = gr.Textbox(
                    value=initial_device_endpoint,
                    label="黄山派网络输出（可选）",
                    placeholder="例如 192.168.31.88:4010；留空仅在电脑播放",
                    info="填写黄山派的 IPv4 和固件监听 UDP 端口。改变地址只重启音频输出，不会断开手机 AirPlay。",
                )
                gr.Markdown("#### 实时多轨混音\n拖动任一滑杆会在不中断声卡输出的情况下平滑更新混音。")
                initial_visibility = active_stem_visibility(initial_profile)
                mixer_sliders = []
                with gr.Row():
                    for stem in STEM_ORDER[:4]:
                        mixer_sliders.append(
                            gr.Slider(
                                minimum=0,
                                maximum=100,
                                value=round(initial_gains[stem] * 100),
                                step=1,
                                label=f"{STEM_LABELS[stem]}音量 (%)",
                                visible=initial_visibility[stem],
                            )
                        )
                with gr.Row():
                    for stem in STEM_ORDER[4:]:
                        mixer_sliders.append(
                            gr.Slider(
                                minimum=0,
                                maximum=100,
                                value=round(initial_gains[stem] * 100),
                                step=1,
                                label=f"{STEM_LABELS[stem]}音量 (%)",
                                visible=initial_visibility[stem],
                            )
                        )
                mixer_status = gr.Markdown("🟢 **已恢复当前混音器状态**")
                with gr.Row():
                    start_live = gr.Button("开始实时捕获", variant="primary")
                    stop_live = gr.Button("停止", variant="stop")
                    refresh_status = gr.Button("刷新状态")
                live_status = gr.Markdown(status_markdown(LIVE_DIR))
                live_visualizer = gr.HTML(live_dashboard_html(LIVE_DIR))
                routing_status = gr.Markdown(routing_markdown(LIVE_DIR))
                gr.Markdown(
                    "AirPlay 接收器随实时会话自动启动。音乐投送通常为 ALAC；"
                    "屏幕镜像音频通常为 AAC-ELD，不属于无损链路。"
                    "同步歌词按歌名、歌手、专辑和时长从 LRCLIB 查询并缓存在本地；"
                    "只发送这些曲目信息，不发送音频。"
                )
                with gr.Row():
                    open_routing = gr.Button("打开 Windows 音量混合器")
                    refresh_routing = gr.Button("刷新纯净监听检测")
                refresh_processes.click(refresh_live_processes, outputs=live_process)
                input_source.change(input_source_changed, inputs=input_source, outputs=live_process)
                mixer_inputs = [live_profile, *mixer_sliders]
                mixer_outputs = [*mixer_sliders, mixer_status]
                live_profile.change(
                    refresh_mixer_sliders,
                    inputs=mixer_inputs,
                    outputs=mixer_outputs,
                    queue=False,
                )
                app.load(
                    restore_live_controls,
                    outputs=[
                        input_source,
                        live_process,
                        live_profile,
                        device_endpoint,
                        *mixer_outputs,
                    ],
                    queue=False,
                )
                for slider in mixer_sliders:
                    slider.input(
                        update_live_mix,
                        inputs=mixer_inputs,
                        outputs=mixer_status,
                        queue=False,
                        trigger_mode="always_last",
                    )
                start_live.click(
                    start_live_capture,
                    inputs=[
                        input_source,
                        live_process,
                        live_profile,
                        device_endpoint,
                        *mixer_sliders,
                    ],
                    outputs=live_status,
                )
                stop_live.click(stop_live_capture, outputs=live_status)
                refresh_status.click(refresh_live_dashboard, outputs=[live_status, live_visualizer])
                live_timer = gr.Timer(0.5)
                live_timer.tick(refresh_live_dashboard, outputs=[live_status, live_visualizer])
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
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    start_acceptance_service(DATA_ROOT / "live")
    start_live_worker(DATA_ROOT / "live")
    start_lyrics_service(LIVE_DIR, LYRICS_DIR)
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
