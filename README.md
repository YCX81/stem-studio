# Stem Studio

Stem Studio 是面向 Windows 与 NVIDIA GPU 的本地音频分离软件。它既能处理完整音频文件，也能自动捕获指定音乐软件及其子进程，持续输出并监听 AI 分离音轨。Web 界面只绑定 `127.0.0.1`，音频不会上传。

## 已实现功能

- 文件分离
  - BS-Roformer 二轨：人声、伴奏
  - `htdemucs_ft` 四轨：人声、鼓、贝斯、其他
  - `htdemucs_6s` 六轨：人声、鼓、贝斯、吉他、钢琴、其他
  - 支持 FLAC、WAV、MP3 输出
- 实时分离
  - Windows 原生进程环回捕获，包含目标进程的子进程
  - 二轨、四轨、六轨模式可切换
  - 可监听任意已生成音轨
  - 12 秒连续窗口；模型常驻 GPU，避免每个窗口重复加载
  - 单个坏窗口会写入错误清单并移动到 `data/live/failed`，后续窗口继续处理
- 本地部署与迁移
  - Docker GPU 后端与浏览器界面
  - 静态链接的 Windows C++ 音频宿主，目标电脑无需安装 Visual Studio
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

1. 先在音乐软件中开始播放。
2. 打开“实时分离”，点击“刷新软件列表”。
3. 选择音乐软件、实时分离模式和要监听的音轨。
4. 点击“开始实时捕获”。
5. 切换模式时，GPU worker 会卸载上一模式并按需加载新模型。

本机 RTX 5060 Ti 8GB 实测的首个结果总延迟：

| 模式 | 12 秒窗口 GPU 处理 | 从采集开始到首结果 |
|---|---:|---:|
| BS-Roformer 二轨 | 约 11.1 秒 | 约 25–30 秒 |
| `htdemucs_ft` 四轨，单 shift | 约 9.5 秒 | 约 22–26 秒 |
| `htdemucs_6s` 六轨，单 shift | 约 3.5–3.7 秒 | 约 15.5–20 秒 |

延迟包括先收集完整 12 秒窗口、GPU 推理、写出音轨和原生播放启动。首次使用某个模型还需要下载权重；下载时间不计入上述热运行数据。

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

导出后会生成 ZIP、对应的 `.sha256` 和 `migration-manifest.json`。复制后先验证：

```powershell
.\scripts\Verify-Package.ps1 -FilePath D:\Transfer\StemStudio-with-models.zip
```

在目标电脑解压 ZIP，安装并启动 Docker Desktop，然后运行：

```powershell
.\scripts\Test-Gpu.ps1
.\scripts\Start.ps1
```

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

`host` 中保留了经过单元测试的中置抑制 DSP 研究模块，但它没有接入默认播放链路。原因是硬消中会误伤同样位于中置的底鼓、贝斯和军鼓；当前预训练模型也不能在不重新训练的情况下真正利用自定义 Mid/Side 先验。产品默认保留原始双声道并使用神经网络分离，后续若训练残差补偿模型，再把 Mid/Side、相干度和中置抑制结果作为额外特征输入。

预训练模型通常在首次使用时由上游下载。若用于公开分发或商业用途，必须分别核实基础镜像、Python 依赖、模型权重和虚拟音频驱动的许可与署名要求；代码许可证不自动等同于模型权重许可证。
