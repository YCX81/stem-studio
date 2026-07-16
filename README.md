# Stem Studio

Stem Studio 是面向 Windows 与 NVIDIA GPU 的本地音频分离软件。它既能处理完整音频文件，也能捕获指定音乐软件，或由源码内置的 UxPlay 1.74 宿主接收手机 AirPlay，将解码 PCM 直接送入 AI 分离链路。Web 界面只绑定 `127.0.0.1`，音频不会上传。

## 已实现功能

- 文件分离
  - BS-Roformer 二轨：人声、伴奏
  - `htdemucs_ft` 四轨：人声、鼓、贝斯、其他
  - `htdemucs_6s` 六轨：人声、鼓、贝斯、吉他、钢琴、其他
  - 支持 FLAC、WAV、MP3 输出
- 实时分离
  - Windows 原生进程环回捕获，包含目标进程的子进程
  - 独立 AirPlay 宿主：UxPlay 1.74 的 RAOP、FairPlay 与 mDNS 源码内置
  - GStreamer 将 ALAC/AAC-ELD 解码为 44.1 kHz、双声道 PCM16LE，直接发布到实时窗口，无 RTP/UDP 中转
  - 二轨、四轨、六轨实时输出模式可切换；三种模式共用常驻 GPU 的 `htdemucs_6s` 六源网络，再按需直出或聚合音轨，文件分离的高质量模型不受影响
  - 可监听任意已生成音轨
  - 12 秒分析窗、6 秒步进、100 ms 同时间轴交叉淡化；持久 WASAPI 队列在窗口间不重开设备
  - PCM 内容寻址窗口缓存，以及按 AirPlay 曲目时间轴构建的连续多轨歌曲缓存；重复曲目校验原声后可绕过 GPU
  - GPU 模型运行在独立子进程中；启动时先完成一整窗 CUDA 推理预热，播放期设有 5.5 秒防卡死硬截止，超时会杀掉子进程并用同帧原声保底
  - 单个坏窗口或实时积压会自动发布同帧长的原始立体声保底，维持宿主时间轴与交叉淡化；诊断输入移入 `data/live/failed`，下一窗继续恢复分轨
- 本地部署与迁移
  - Docker GPU 后端与浏览器界面
  - Windows 进程音频宿主与自包含 AirPlay/GStreamer 运行时，目标电脑无需安装 Visual Studio、MSYS2 或 UxPlay
  - 模型缓存、Docker 镜像可选打包，迁移文件附带 SHA256

## 本机要求

1. Windows 10/11
2. 支持 CUDA 的 NVIDIA 显卡与较新的 NVIDIA 驱动
3. Docker Desktop，启用 WSL2 后端与 GPU 支持
4. 启动 Docker Desktop

Visual Studio C++ 仅在重新编译原生宿主时需要；正常运行和迁移后的目标电脑不需要它。

## 启动与停止

在项目目录的 PowerShell 中运行：

```powershell
.\scripts\Test-Gpu.ps1
.\scripts\Start.ps1
```

浏览器随后打开：

```text
http://127.0.0.1:7860/
```

停止：

```powershell
.\scripts\Stop.ps1
```

查看容器日志：

```powershell
docker compose logs -f app
```

## 实时分离使用方法

### Windows 音乐软件

1. 先在音乐软件中开始播放。
2. 打开“实时分离”，点击“刷新软件列表”。
3. 选择音乐软件、实时分离模式和要监听的音轨。
4. 点击“开始实时捕获”。
5. 切换二/四/六轨实时输出模式时会复用同一个常驻六源模型；内部命令名保持兼容，界面不会把实时二轨误标成离线 BS-Roformer 高质量模式。

### 手机 AirPlay 音频

手机与电脑必须位于允许 mDNS 和设备互访的同一局域网。启动 Stem Studio 后：

1. 在“音频来源”选择“手机 AirPlay → 内置接收器”，选择分离模式和监听音轨，点击开始。
2. 在 iPhone/iPad 控制中心的 AirPlay 音频设备列表选择 `StemStudio`。
3. 使用音乐 App 的音频投送，不要选择“屏幕镜像”。音频模式通常以 ALAC 输入 UxPlay；屏幕镜像的音频通常是 AAC，无法称为端到端无损。

#### AirPlay 缓冲与本地 AI 缓存不是一回事

- AirPlay/RAOP 是连续 RTP 流，没有“按歌曲缓存整首”的机制。当前 UxPlay 1.74 默认向发送端申报约 `0.25 秒`音频延迟，命令行可配置范围为 `0–10 秒`；它用于网络抖动与播放同步，不能代替 AI 推理缓存。
- Stem Studio 仍会在首播时边收、边分离、边播放；同时把已交叉拼接的原声与二/四/六轨按歌曲时间轴写成连续 PCM16 WAV。完整曲目重播时，先用当前 PCM 在候选歌曲原声中做精确对齐，再从任意歌曲位置切出所需多轨范围；校验命中后不加载模型、不调用 GPU 分离。
- 界面始终显示磁盘中已完成的整曲缓存数量；该数量来自持久缓存清单，Docker 或桌面宿主重启后不会归零。相同音频重复建库会按内容指纹去重，不会虚增歌曲数量。
- 歌名、艺术家、专辑和时长只用于缩小候选范围，不能单独触发命中。AirPlay 在循环或换歌开头若短暂沿用上一首的错误时长，实时复播路径可以在歌名、艺术家和专辑一致时进入候选，但仍必须通过当前 PCM 强校验；同名不同版本可以并存，PCM 对不上就安全回退到 GPU。手机音量或服务端母带变化若改变了解码 PCM，也会视为未命中。
- 实时是否可持续取决于“单窗 GPU 处理时间是否小于 `6 秒`步进期限”。界面实时显示输入波形、待处理窗口、可播放缓存、欠载次数、单窗耗时、缓存命中和原声保底次数，还会显示隔离推理 PID、模型完整预热状态、5.5 秒硬截止、预热/超时保底计数和最慢窗口；正在播放时若积压超过一窗，会优先用原声保底追平实时队列，避免持续过载耗尽预缓冲。
- Windows 默认输出设备切换、休眠恢复或音频服务短暂重启时，WASAPI 播放层会保留多轨队列并按 `100 ms–1 s` 退避自动重开端点；AirPlay 接收和已完成的本地分轨缓存不会随之重启。
- Windows 控制器每约 2 秒写入独立心跳；活动会话超过 6 秒未刷新时，界面会明确标记控制器离线，避免把过期的“等待/播放”状态当成真实在线。启动脚本会先确认 Docker 引擎及容器启动成功，再轮换现有控制器与原生宿主，构建失败不会先拆掉仍可用的音频链。
- 本地缓存默认总上限为 20 GiB：约 15 GiB 留给连续歌曲、5 GiB 留给无曲目时间轴时的精确窗口缓存，并按最近使用顺序清理。PCM16 立体声三分钟歌曲约占：二轨（原声+2轨）95 MiB、四轨约159 MiB、六轨约222 MiB。
- 内置宿主将解码 PCM 与原子时间轴侧车写入 `data/live/inbox`，Docker GPU worker 只读取完整窗口；手机不连接 Docker，也不使用额外 UDP 端口。

真机产品验收可运行 `python tools/monitor_live_acceptance.py --timeout-seconds 1800`。监视器要求手机完整首播产生 GPU 窗口和整曲缓存、同曲重播命中歌曲缓存、播放期间欠载/声卡重连/分轨跳窗全部保持为零，并在真实音频播放时至少收到一次不高于 50 ms 的滑杆更新；外部监视结果会持续原子写入 `data/live/acceptance-monitor-report.json`，不会再与桌面端内置的 `data/live/acceptance-report.json` 争用临时文件或相互覆盖。同一项目只能有一份外部监视器持有 OS 独占锁，重复启动会返回 `already_running` 和现有 PID，异常退出后锁会由操作系统自动释放。暂停或断流阶段的队列自然排空、端点变化和历史跳窗不会污染下一次活跃播放段的验收基线。

### 缓存播放期间无感更新 Docker 应用

先构建候选镜像，但不要直接重启正在播放的容器：

```powershell
docker compose build app
.\scripts\Update-AppBuffered.ps1 -PlanOnly
```

只读预检会等待最多 30 秒寻找安全窗口，并要求当前为歌曲缓存命中、原生播放正常、设备不在恢复中、待播队列不积压且缓冲不少于 15 秒。确认 `state` 为 `ready` 后执行：

```powershell
.\scripts\Update-AppBuffered.ps1
```

脚本会在替换 `app` 容器前保留候选和回滚镜像标签；Windows AirPlay 与 WASAPI 宿主不重启，继续消费已有多轨缓冲。新容器必须通过 Docker 健康检查，并在时限内发布至少一个整曲缓存命中且保持零 cache miss、零原声保底；AirPlay PCM 与原生播放序列必须继续前进，欠载、声卡恢复和分轨跳窗计数均不得变化。任一条件失败会自动恢复旧镜像并重建旧容器，结果写入 `data/temp/buffered-app-update-state.json`。

### 构建 UxPlay 1.74

仓库已固定并内置 UxPlay 1.74 commit `3ca7526387e894d6848b84c209de361c3bedd1ec`。重新构建开发版需要 `C:\msys64` 的 UCRT64 工具链与 UxPlay 所需依赖：

```powershell
.\scripts\Build-AirPlayHost.ps1
```

脚本会解决中文路径构建问题，输出 `airplay-host/bin/stem-studio-airplay-host.exe`，递归收集运行时 DLL，并打包最小 GStreamer 插件集。正常使用和迁移包不要求目标电脑安装 MSYS2。

### 安全部署原生宿主候选

开发构建应先输出到 `data/temp/audio-host-next` 与 `data/temp/airplay-host-next`，不要覆盖正在播放的宿主。部署前可做完全只读的检查：

```powershell
.\scripts\Install-NativeHosts.ps1 -ReadinessOnly
```

活动 PCM 会返回 `ActiveStream`。暂停手机播放并保持 PCM 帧计数至少 5 秒不增长后，再运行安装器；也可通过 `-ExpectedAudioHash` 与 `-ExpectedAirPlayHash` 固定本次批准的候选哈希。安装器先把完整 AirPlay/GStreamer 包复制到同盘事务目录并校验递归文件清单，随后只停止两个原生宿主和 Controller，以目录重命名完成切换，再恢复原来的二/四/六轨配置。Docker、GPU worker、歌曲缓存和播放队列目录不会重启或删除。新宿主启动、进程优先级或哈希验证失败时会自动恢复旧音频宿主与完整 AirPlay 包；事务与回滚结果记录在 `data/temp/native-update-state.json`。

本机 RTX 5060 Ti 8GB、12 秒真实捕获窗口的常驻模型稳态实测：

| 实时输出模式 | 12 秒窗口处理 | 相对 6 秒步进余量 |
|---|---:|---:|
| 二轨：人声 / 伴奏（六源聚合） | 1.661 秒 | 4.339 秒 |
| 四轨：人声 / 鼓 / 贝斯 / 其他（六源聚合） | 1.991 秒 | 4.009 秒 |
| 六轨完整分轨 | 1.411 秒 | 4.589 秒 |

表中包含常驻网络推理、源轨聚合和 WAV 发布。冷启动的完整 12 秒 CUDA 预热实测为 10.77–14.75 秒，只在隔离子进程启动时发生；预热完成前若已经收到音频，系统发布原声保底而不阻塞时间轴。相同 PCM、模型和轨数第二次重放的内容缓存实测为 `0.0 秒 / 0 GPU`。首次下载模型的网络时间不计入这些数据。

三分钟、六轨、44.1 kHz PCM16 的持久歌曲缓存 I/O 基准占用 `222,266,059` 字节：进程重启后的首次清单校验、PCM 对齐与六轨窗口发布合计 `0.146 秒`，随后 10 次热缓存发布中位数 `0.012 秒`、最慢 `0.014 秒`，均远低于 6 秒实时期限。可用 `python tools/benchmark_song_cache.py --duration-seconds 180 --repeats 10` 复测；基准使用真实缓存几何和文件体积，临时数据结束后自动清理。

### 只听分离结果（纯净监听）

Windows 的进程环回捕获位于应用音量之后。本机验证表明：直接静音目标音乐软件也会让捕获信号变成静音，因此不能通过“自动静音原播放器”实现纯净监听。

正确做法是使用虚拟音频设备：

1. 安装可信的虚拟音频设备，例如 VB-CABLE、VoiceMeeter 或同类方案。
2. 在 Stem Studio 点击“打开 Windows 音量混合器”。
3. 将目标音乐软件的输出设备改为虚拟设备（例如 `CABLE Input`）。
4. 保持 Windows 默认输出为实际耳机或扬声器。
5. 返回 Stem Studio，刷新纯净监听检测并开始捕获。

虚拟音频驱动没有随项目捆绑，避免驱动签名、授权和管理员安装带来的迁移风险。没有虚拟设备时，捕获和 AI 分离仍能工作，但原声与分离监听会同时播放。

## 文件分离实测

测试文件《恋人.mp3》：4 分 35.95 秒、48 kHz 双声道、320 kbps；产品默认文件质量参数为 2 shifts。

| 模式 | 输出轨数 | 本机耗时 | 相对实时速度 |
|---|---:|---:|---:|
| `htdemucs_ft` 四轨高质量 | 4 | 78.5 秒 | 约 3.5 倍实时 |
| `htdemucs_6s` 六轨 | 6 | 27.8 秒 | 约 9.9 倍实时 |

六轨更快不是因为质量一定更高，而是它使用单个六源模型；四轨 `htdemucs_ft` 是更重的微调模型组合。应按需要的音轨和听感选择，不应只按轨数判断质量。

## 数据目录

- `data/models`：模型缓存
- `data/outputs`：文件分离结果
- `data/temp`：临时文件
- `data/live/inbox`：原生捕获窗口
- `data/live/outbox`：实时 stem 与结果清单
- `data/live/failed`：被隔离的坏窗口及其残留输出
- `data/live/song-cache`：校验完整的连续歌曲原声与多轨缓存
- `data/live/cache`：缺少可靠曲目时间轴时使用的 PCM 内容寻址窗口缓存

删除或重建容器不会删除这些挂载目录。迁移包默认不包含私人输出和实时捕获。

## 迁移到另一台电脑

### 联网迁移

只迁移程序，让目标电脑重新下载模型：

```powershell
.\scripts\Export-Migration.ps1 -Destination D:\Transfer
```

同时携带已缓存模型：

```powershell
.\scripts\Export-Migration.ps1 -Destination D:\Transfer -IncludeModels
```

导出后会生成 ZIP、对应的 `.sha256` 和 `migration-manifest.json`。ZIP 包含自包含 AirPlay 运行时、固定的 UxPlay 源码与许可证、验收文档和 `tools/monitor_live_acceptance.py`，但会剔除所有 `*.next.exe` 开发候选以及 `data/live`、`data/temp`、私人输出。清单同时记录实际交付的音频宿主 EXE、AirPlay EXE 与完整 AirPlay/GStreamer 包哈希，不包含源电脑名。复制后先验证：

```powershell
.\scripts\Verify-Package.ps1 -FilePath D:\Transfer\StemStudio-with-models.zip
```

在目标电脑解压 ZIP，安装并启动 Docker Desktop，然后运行：

```powershell
.\scripts\Verify-NativeRuntime.ps1 -Root . -ManifestPath .\native-runtime-manifest.json
.\scripts\Test-Gpu.ps1
.\scripts\Start.ps1
```

迁移 ZIP 内置的 `native-runtime-manifest.json` 会被 `Start.ps1` 自动复核；手工执行验证命令便于在启动前直接看到具体不匹配的 EXE 或完整 AirPlay/GStreamer 包。验证失败发生在停止任何现有宿主之前。

### 完全离线迁移

```powershell
.\scripts\Export-Migration.ps1 -Destination D:\Transfer -IncludeModels -IncludeImage
```

除 ZIP 外还会生成 `stem-studio-image.tar` 及其 SHA256。目标电脑解压程序 ZIP 后运行：

```powershell
.\scripts\Verify-Package.ps1 -FilePath D:\Transfer\stem-studio-image.tar
.\scripts\Import-Offline.ps1 -ImagePath D:\Transfer\stem-studio-image.tar
```

`Import-Offline.ps1` 在存在 sidecar 时会再次校验 SHA256，然后加载镜像并以 `-NoBuild` 启动。

## 故障排查

- GPU 检测失败：运行 `.\scripts\Test-Gpu.ps1`，确认 Docker Desktop 使用 WSL2 后端。
- 列表没有音乐软件：先播放音乐，再点击“刷新软件列表”。
- 原声仍能听到：这是未配置虚拟音频路由，不是分离失败。
- 某窗口失败：查看 `data/live/gpu-status.json` 与 `data/live/failed`；worker 会继续下一窗口。
- 容器启动失败：运行 `docker compose logs --tail 100 app`。
- 模型切换时显存不足：停止实时会话后重启容器；8GB 显存下不要并发运行文件任务与实时任务。

## 技术与许可说明

- 基础镜像：PyTorch 2.12 / CUDA 13.0 / cuDNN 9
- 分离引擎：audio-separator 0.44.3
- 本地界面：Gradio 6.20.0
- Windows 捕获：`ActivateAudioInterfaceAsync` 进程环回
- 原生播放：WinMM
- 手机音频接收：修改版 UxPlay 1.74 / GStreamer `appsink` PCM 直通

`host` 中保留了经过单元测试的中置抑制 DSP 研究模块，但它没有接入默认播放链路。原因是硬消中会误伤同样位于中置的底鼓、贝斯和军鼓；当前预训练模型也不能在不重新训练的情况下真正利用自定义 Mid/Side 先验。产品默认保留原始双声道并使用神经网络分离，后续若训练残差补偿模型，再把 Mid/Side、相干度和中置抑制结果作为额外特征输入。

内置 UxPlay 及与其链接的 AirPlay 宿主按 GNU GPL v3 分发；固定上游源码、修改内容和许可证位于 `third_party/UxPlay`，迁移包同时携带对应源码。预训练模型通常在首次使用时由上游下载。若用于公开分发或商业用途，仍必须分别核实基础镜像、Python 依赖、模型权重和虚拟音频驱动的许可与署名要求；代码许可证不自动等同于模型权重许可证。
