$ErrorActionPreference = 'Stop'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw '未找到 Docker。请先安装 Docker Desktop。'
}

docker info | Out-Null
docker run --rm --gpus all pytorch/pytorch:2.12.0-cuda13.0-cudnn9-runtime `
    python -c "import torch; assert torch.cuda.is_available(); print('GPU:', torch.cuda.get_device_name(0)); print('CUDA:', torch.version.cuda); print('Capability:', torch.cuda.get_device_capability(0))"

if ($LASTEXITCODE -ne 0) {
    throw 'GPU 容器自检失败。确认 Docker Desktop 使用 WSL2 后端，并已启用 GPU 支持。'
}
