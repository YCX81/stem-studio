from __future__ import annotations

import os
from pathlib import Path
import threading

import gradio as gr

from .acceptance_service import start_acceptance_service
from .core import LIVE_PROFILES, MODEL_PROFILES, SeparationRequest
from .engine import AudioSeparatorEngine
from .gpu_work import GpuReservation, GpuWorkCoordinator
from .hardware import (
    HardwareConfig,
    apply_hardware_config,
    detect_hardware_config,
    write_hardware_profile,
)
from .live_worker import start_live_worker
from .lyrics import start_lyrics_service
from .live_control import (
    STEM_LABELS,
    active_stem_visibility,
    live_dashboard_html,
    live_pipeline_snapshot,
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
_HARDWARE_CONFIG: HardwareConfig | None = None
_GPU_COORDINATOR: GpuWorkCoordinator | None = None
_LIVE_RESERVATION: GpuReservation | None = None
_RESERVATION_LOCK = threading.Lock()

CSS = """
.gradio-container { max-width: 1120px !important; }
.hero { padding: 28px 6px 12px; }
.hero h1 { font-size: 2.1rem; margin-bottom: 6px; }
.hero p { color: #6b7280; font-size: 1.04rem; }
.status-card { border-radius: 14px; }
"""


def _hardware_config() -> HardwareConfig:
    global _HARDWARE_CONFIG
    if _HARDWARE_CONFIG is None:
        _HARDWARE_CONFIG = detect_hardware_config()
    return _HARDWARE_CONFIG


def _gpu_coordinator() -> GpuWorkCoordinator:
    global _GPU_COORDINATOR
    if _GPU_COORDINATOR is None:
        _GPU_COORDINATOR = GpuWorkCoordinator(_hardware_config().gpu_concurrency)
    return _GPU_COORDINATOR


def _native_live_session_active() -> bool:
    snapshot = live_pipeline_snapshot(LIVE_DIR)
    return snapshot["controller_online"] and snapshot["controller_state"] in {
        "capturing",
        "airplay_waiting",
    }


def _gpu_status_markdown() -> str:
    config = _hardware_config()
    if not config.available:
        return f"🔴 **GPU 未就绪** · {config.warnings[0]}"
    warning = f" · ⚠️ {config.warnings[0]}" if config.warnings else ""
    return (
        f"🟢 **GPU 已就绪** · {config.device_name} · {config.vram_gb:g} GB · "
        f"{config.tier} 档 · 文件并发 {config.gpu_concurrency} · "
        f"实时 12 秒窗/{config.live_hop_seconds} 秒步进/shifts≤{config.demucs_shifts}"
        f"{warning}"
    )


def run_separation(
    source: str | None,
    profile_name: str,
    output_format: str,
) -> tuple[str, list[str]]:
    if not source:
        return "请先拖入或选择一个音频文件。", []
    reservation: GpuReservation | None = None
    try:
        if _native_live_session_active():
            raise RuntimeError("原生实时宿主仍在运行，请先停止实时捕获。")
        reservation = _gpu_coordinator().reserve_file()
        config = _hardware_config()
        request = SeparationRequest.create(
            source=source,
            profile_name=profile_name,
            output_format=output_format,
            output_root=OUTPUT_DIR,
        )
        outputs = AudioSeparatorEngine(
            MODEL_DIR,
            mdxc_segment_size=config.mdxc_segment_size,
        ).separate(request)
        message = (
            f"✅ 分离完成：生成 {len(outputs)} 条音轨。\n\n"
            f"保存目录：`{request.output_dir}`"
        )
        return message, [str(path) for path in outputs]
    except Exception as exc:
        return f"❌ 分离失败：{exc}", []
    finally:
        if reservation is not None:
            reservation.release()


def refresh_live_processes():
    choices = read_processes(LIVE_DIR)
    return gr.Dropdown(choices=choices, value=0)


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


def switch_live_profile(
    profile_name: str,
    vocals: float,
    instrumental: float,
    drums: float,
    bass: float,
    guitar: float,
    piano: float,
    other: float,
):
    """Apply a profile selection to both the mixer UI and an active native host."""
    slider_outputs = refresh_mixer_sliders(
        profile_name,
        vocals,
        instrumental,
        drums,
        bass,
        guitar,
        piano,
        other,
    )
    if not _native_live_session_active():
        return (*slider_outputs, status_markdown(LIVE_DIR))

    restored = live_ui_defaults(LIVE_DIR)
    live_message = start_live_capture(
        restored["input_source"],
        restored["process_id"],
        profile_name,
        restored["device_endpoint"],
        vocals,
        instrumental,
        drums,
        bass,
        guitar,
        piano,
        other,
    )
    if live_message.startswith("🔴"):
        return (*slider_outputs[:-1], live_message, live_message)

    track_count = len(LIVE_PROFILES[profile_name].stems)
    mixer_message = (
        f"🟡 **{track_count} 轨推子已写入** · 音频链路正在切换"
    )
    live_message = (
        f"🟡 **正在切换至 {profile_name}** · "
        "音频宿主正在重新缓冲，约 15–20 秒"
    )
    return (*slider_outputs[:-1], mixer_message, live_message)


def input_source_changed(input_source: str):
    return gr.Dropdown(visible=input_source == "process")


def restore_live_controls():
    restored = live_ui_defaults(LIVE_DIR)
    input_source = restored["input_source"]
    profile_name = restored["profile_name"]
    allowed_names = [
        name
        for name, profile in LIVE_PROFILES.items()
        if len(profile.stems) <= max(2, _hardware_config().max_live_tracks)
    ]
    if profile_name not in allowed_names:
        profile_name = allowed_names[0]
    device_endpoint = restored["device_endpoint"]
    gains = restored["gains"]
    visibility = active_stem_visibility(profile_name)
    process_choices = read_processes(LIVE_DIR) if input_source == "process" else []
    available_process_ids = {choice[1] for choice in process_choices}
    restored_process_id = restored.get("process_id")
    process_value = (
        restored_process_id
        if restored_process_id in available_process_ids
        else process_choices[0][1]
        if process_choices
        else None
    )
    return (
        gr.Radio(value=input_source),
        gr.Dropdown(
            choices=process_choices,
            value=process_value,
            visible=input_source == "process",
        ),
        gr.Dropdown(value=profile_name, choices=allowed_names),
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
    global _LIVE_RESERVATION
    created_reservation = False
    try:
        with _RESERVATION_LOCK:
            if _LIVE_RESERVATION is None:
                _LIVE_RESERVATION = _gpu_coordinator().reserve_live()
                created_reservation = True
        config = _hardware_config()
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
            hop_seconds=config.live_hop_seconds,
        )
        latency = {
            "人声 / 伴奏 · 高质量": "约 15–20 秒",
            "四轨 · 人声/鼓/贝斯/其他": "约 15–20 秒",
            "六轨 · 加吉他/钢琴": "约 15–20 秒",
        }[profile_name]
        if input_source == "airplay":
            return (
                "🟡 **正在启动内置 AirPlay 接收器** · 在手机控制中心选择 Stem Studio · "
                f"首个 AI 结果{latency}"
            )
        return f"🟡 **正在启动 {profile_name}** · 首个 AI 结果{latency}"
    except Exception as exc:
        if created_reservation:
            with _RESERVATION_LOCK:
                if _LIVE_RESERVATION is not None:
                    _LIVE_RESERVATION.release()
                    _LIVE_RESERVATION = None
        return f"🔴 **启动失败** · {exc}"


def stop_live_capture() -> str:
    global _LIVE_RESERVATION
    write_command(LIVE_DIR, "stop")
    with _RESERVATION_LOCK:
        if _LIVE_RESERVATION is not None:
            _LIVE_RESERVATION.release()
            _LIVE_RESERVATION = None
    return "⚪ **已发送停止命令**"


def open_audio_settings() -> str:
    write_command(LIVE_DIR, "open_audio_settings")
    return (
        "🟡 **已请求打开 Windows 音量混合器** · "
        "将目标音乐软件的输出设备改为虚拟音频设备，然后返回刷新检测。"
    )


def build_app() -> gr.Blocks:
    config = _hardware_config()
    allowed_live_profiles = {
        name: profile
        for name, profile in LIVE_PROFILES.items()
        if len(profile.stems) <= max(2, config.max_live_tracks)
    }
    initial_live = live_ui_defaults(LIVE_DIR)
    initial_source = initial_live["input_source"]
    initial_profile = initial_live["profile_name"]
    if initial_profile not in allowed_live_profiles:
        initial_profile = next(iter(allowed_live_profiles))
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
                    "可捕获全部 Windows 音频（推荐）或单个进程，也可由内置 UxPlay 1.74 宿主接收手机 AirPlay，解码 PCM 后直接分离。"
                    f"支持二轨、四轨或六轨实时输出模式；采用 12 秒分析窗、{config.live_hop_seconds} 秒步进和 100ms 同时间轴交叉拼接。"
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
                        label="Windows 音频捕获范围",
                        choices=[("全部 Windows 音频（推荐）", 0)],
                        value=0,
                        visible=initial_source == "process",
                    )
                    refresh_processes = gr.Button("刷新软件列表")
                live_profile = gr.Dropdown(
                    choices=[
                        (profile.display_name, profile_name)
                        for profile_name, profile in allowed_live_profiles.items()
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
                live_profile.input(
                    switch_live_profile,
                    inputs=mixer_inputs,
                    outputs=[*mixer_outputs, live_status],
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
            f"当前硬件自动允许 {config.gpu_concurrency} 个文件任务并发；"
            "实时捕获期间会独占 GPU，避免文件任务争抢显存和截止时间。"
        )
        run_button.click(
            fn=run_separation,
            inputs=[source, profile, output_format],
            outputs=[status, outputs],
            concurrency_limit=config.gpu_concurrency,
        )
    return app


def main() -> None:
    global _HARDWARE_CONFIG, _GPU_COORDINATOR
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    _HARDWARE_CONFIG = detect_hardware_config()
    apply_hardware_config(_HARDWARE_CONFIG)
    write_hardware_profile(DATA_ROOT, _HARDWARE_CONFIG)
    _GPU_COORDINATOR = GpuWorkCoordinator(_HARDWARE_CONFIG.gpu_concurrency)
    start_acceptance_service(DATA_ROOT / "live")
    start_live_worker(
        DATA_ROOT / "live",
        inference_timeout_seconds=_HARDWARE_CONFIG.inference_timeout_seconds,
        live_hop_seconds=_HARDWARE_CONFIG.live_hop_seconds,
        demucs_shifts=_HARDWARE_CONFIG.demucs_shifts,
        shifts_benchmark_limit_seconds=(
            _HARDWARE_CONFIG.shifts_benchmark_limit_seconds
        ),
    )
    start_lyrics_service(LIVE_DIR, LYRICS_DIR)
    app = build_app()
    app.queue(default_concurrency_limit=_HARDWARE_CONFIG.gpu_concurrency).launch(
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
