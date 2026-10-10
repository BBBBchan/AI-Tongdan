# 从下载代码到独立部署

本文面向新机器，不依赖作者的服务地址、任务调度平台、Python 环境或已下载权重。应用服务器可以运行在 Linux/macOS；本文提供的两个模型服务使用 Linux + NVIDIA CUDA。macOS 可运行网页、取流、抽帧与音频处理，再通过网络连接 Linux 上的模型服务。

仓库只包含源代码、配置模板和第三方许可。模型权重、API 密钥、B 站 Cookie、会话数据、虚拟环境和运行日志都由部署者自行生成。Cookie 设置见 [绑定自己的 Bilibili Cookie](../README.md#绑定自己的-bilibili-cookie)。模型服务与主应用是三个独立进程，安装时使用三个独立虚拟环境。

## 1. 先决定运行哪些组件

| 组件 | 做什么 | Python 与系统依赖 | 默认监听地址 |
| --- | --- | --- | --- |
| 主应用 | 网页、B 站房间信息与弹幕、FFmpeg 取流、观察调度、记忆和问答 | Python 3.10+、FFmpeg；Linux/macOS | `127.0.0.1:7862` |
| 视觉服务 | Qwen3.8-27B-FP8，多帧观察及问答生成 | 独立 Python 3.11 环境、vLLM、兼容的 Linux NVIDIA GPU/驱动 | `127.0.0.1:8020` |
| 音频服务 | Qwen3-ASR-0.6B 转写、Silero VAD、CAM++ 声纹 | 独立 Python 3.11 环境、PyTorch CUDA；GPU 支持 BF16 | `127.0.0.1:8021` |

已经有兼容视觉与音频服务的用户，只需安装主应用，再按第 7 节填入自己服务的地址与密钥。没有模型服务时页面仍可启动，但“服务已启动”不代表视觉观察或音频识别已经可用。

视觉 API 必须同时支持 Chat Completions、多张 `image_url` 数据 URI、`response_format: {"type":"json_object"}` 和请求中的 `chat_template_kwargs.enable_thinking=false`。应用不是对任意文字聊天 API 都兼容。音频使用本仓库 `/analyze` 协议；只有 Whisper 风格 `/v1/audio/transcriptions` 的服务不能直接填入 `AUDIO_BASE_URL`。

本文的模型参数沿用项目已有的 H200 部署配置；发布用的便携启动器没有重新执行完整下载、全新 GPU 环境安装和模型启动。本文不会将 CPU 检查、健康响应或历史部署结果称为新机器上的端到端验证。新机器需完成第 8 节验收。

## 2. 下载代码，安装主应用

私有仓库必须先获得访问权限。通过自己配置好的 GitHub SSH 身份下载：

```bash
git clone git@github.com:BBBBchan/AI-Tongdan.git
cd AI-Tongdan
```

也可以在已登录 GitHub 的浏览器中下载 ZIP 并解压。仅从仓库取得源代码不会下载模型。后续命令除特别注明外，都在仓库根目录执行。

Ubuntu/Debian 示例：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git ffmpeg
python3 --version
ffmpeg -version
ffprobe -version
```

确认 Python 至少为 3.10；旧版操作系统的 `python3` 不一定满足要求。模型环境的示例另用 `python3.11`，请通过系统包管理器或自己选用的 Python 管理器准备该解释器。

macOS，在已安装 Homebrew 的前提下：

```bash
brew install python@3.11 ffmpeg git
python3.11 --version
ffmpeg -version
ffprobe -version
```

创建主应用环境。Linux 可用 `python3`，macOS 可将第一行换为 `python3.11 -m venv .venv`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

已有 `.env` 时保留原文件，按模板补充配置。主应用不需要安装 PyTorch、vLLM 或 Qwen-ASR；调用远程模型不会把模型权重加载进 `.venv`。

启动应用：

```bash
HOST=127.0.0.1 PORT=7862 ./start.sh
```

浏览器打开 `http://127.0.0.1:7862`。启动脚本只在 `.venv` 不存在时首次安装依赖；更新 `requirements.txt` 后，应主动重新执行 `pip install -r requirements.txt`。`HOST`/`PORT` 建议像上面一样从 shell 显式传入，避免把仅供 Python 读取的 `.env` 配置误当成 shell 环境。

若 FFmpeg 不在 PATH 中，在 `.env` 的 `FFMPEG_BIN` 填入你自己的可执行文件路径。音轨时间对齐还需要 `ffprobe`，应放在 FFmpeg 同一目录或 PATH 中。程序需要这些可执行文件本身，安装名称相似的 Python 包不能代替它们。

## 3. 模型与硬件准备

| 用途 | 上游仓库 | 固定 revision | 本地目录 |
| --- | --- | --- | --- |
| 视觉、回答生成 | [Qwen/Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) | `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` | `data/models/Qwen3.8-27B-FP8` |
| ASR | [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B) | `5eb144179a02acc5e5ba31e748d22b0cf3e303b0` | `data/models/Qwen3-ASR-0.6B` |
| 声纹 | [funasr/campplus](https://huggingface.co/funasr/campplus) | `e4b6ede7ce16997aff4ae69fbca1f0175e2afede` | `data/models/campplus` |

下载脚本从 [deploy/models.json](../deploy/models.json) 读取版本，不会默认追踪变化中的 `main` 分支。改变 revision 应作为一次独立的模型升级，并重新进行功能检查。

CAM++ 的两份实现文件已经随代码包含在 `deploy/audio/vendor/`，原始仓库是 [modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker)，版本及 SHA256 在 [SOURCE.json](../deploy/audio/vendor/SOURCE.json)，Apache-2.0 许可随目录保留。Silero VAD 权重由 `silero-vad` 包提供，因此不在模型下载列表中。该部署没有安装 Forced Aligner 模型。

视觉权重本身约 31 GB，还需要模型缓存、CUDA 内核、虚拟环境和会话数据的磁盘空间；显存还包括 KV cache、多图视觉处理、激活值与运行时开销。不能用权重磁盘大小直接判断 GPU 显存是否足够。本项目没有给出经验证的最低显存卡型；已有运行参考为 H200，不能据此保证小显存 GPU 使用相同参数也能启动。FP8 权重需要推理引擎和 GPU 支持相应算子；音频脚本明确检查 CUDA/BF16。

在 GPU 主机执行：

```bash
nvidia-smi
df -h .
```

检查驱动、GPU 型号、空闲显存和磁盘。vLLM/PyTorch wheel 与 CUDA 驱动要兼容。安装准则见 [vLLM GPU 安装文档](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)；不同版本 wheel 使用的 CUDA 版本会变化，不要在原有研究环境里直接升级它们。

### 下载全部或部分模型

下载不需要 GPU，可在联网的主应用主机或 GPU 主机上完成：

```bash
python3 -m venv .venv-download
.venv-download/bin/python -m pip install --upgrade pip
.venv-download/bin/python -m pip install -r deploy/requirements-download.txt
.venv-download/bin/python deploy/download_models.py --component all --dry-run
.venv-download/bin/python deploy/download_models.py --component all
```

按需只下载某一组件：

```bash
.venv-download/bin/python deploy/download_models.py --component vision
.venv-download/bin/python deploy/download_models.py --component audio
```

下载脚本使用 Hugging Face `snapshot_download`，保留配置、tokenizer、权重与许可，限制四个并行下载任务。中断后重新执行同一条命令，Hub 会复用已取得的文件；下载及缓存行为见 [官方说明](https://huggingface.co/docs/huggingface_hub/guides/download)。权重成功取得后会在模型目录写入下载记录；这不等于模型已成功加载。

如果默认磁盘空间不够，传入自己选择的目录，例如：

```bash
.venv-download/bin/python deploy/download_models.py --component all --model-dir "$HOME/vtuber-models"
```

启动模型时需要对应设置 `VISION_MODEL_DIR`、`AUDIO_MODEL_DIR` 和 `SPEAKER_MODEL_FILE`，见后面的变量表。相对 `--model-dir` 从运行命令时的当前目录解析。启动器的模型默认路径则相对于仓库自身，移动整个代码目录不需要改源码。

这些公开模型通常无需登录；若你的网络环境或 Hub 访问策略需要凭据，使用部署者自己的 Hugging Face token：

```bash
.venv-download/bin/hf auth login
```

下载失败先检查网络、代理、磁盘及所选 revision 是否可访问。不要把 Hugging Face token、B 站 Cookie 或模型服务密钥写进仓库。离线机器可复制完整的 `data/models/` 目录以及适配目标系统的 Python wheel 缓存；只复制 `.safetensors` 不足以加载 tokenizer 和视觉处理器。

## 4. 创建本机服务密钥

在将要运行模型服务的机器上执行：

```bash
python3 deploy/configure_local.py
```

该脚本使用系统安全随机数生成两个不同密钥，并以权限 `0600` 创建以下文件：

```text
data/secrets/vision_api_key.txt
data/secrets/audio_api_key.txt
data/model_endpoint.json
data/audio_endpoint.json
```

端点文件引用相对它自身的 `secrets/...` 路径，不包含密钥文本。所有现有文件都会保留，不会替你覆盖正在使用的服务配置；若存在旧配置，先检查内容并自行决定是否修改。脚本创建的是配置，不会验证服务是否在线。

默认地址适用于主应用与模型同机，或第 7 节所述的 SSH 转发。若使用其他端口，端点文件也要同步修改。采用 `--data-dir` 自定义目录时，还需将应用 `DATA_DIR`、`MODEL_ENDPOINT_FILE`、`AUDIO_ENDPOINT_FILE` 和模型服务的密钥文件变量指向这个目录；此参数不会自动修改启动器环境。例如初始化到 `/your/private/vtuber-data` 后，在主应用 `.env` 中设置：

```dotenv
DATA_DIR=/your/private/vtuber-data
MODEL_ENDPOINT_FILE=/your/private/vtuber-data/model_endpoint.json
AUDIO_ENDPOINT_FILE=/your/private/vtuber-data/audio_endpoint.json
```

`.env.example` 显式指定了两个 endpoint 路径，所以只修改 DATA_DIR 不够。也可以从 `.env` 完全删除这两个 endpoint 变量，让应用使用 DATA_DIR 下的默认文件名。启动视觉、音频服务时，另外设置 `VISION_API_KEY_FILE=/your/private/vtuber-data/secrets/vision_api_key.txt` 和 `AUDIO_API_KEY_FILE=/your/private/vtuber-data/secrets/audio_api_key.txt`；模型权重路径由各自的模型路径变量控制。

## 5. 安装和启动视觉服务

在 Linux GPU 主机上建立全新的视觉环境：

```bash
python3.11 -m venv .venv-vision
.venv-vision/bin/python -m pip install --upgrade pip
.venv-vision/bin/python -m pip install -r deploy/vision/requirements.txt
.venv-vision/bin/python -m pip check
.venv-vision/bin/python -c 'import torch, vllm, transformers; print(torch.__version__, vllm.__version__, transformers.__version__); print("CUDA:", torch.cuda.is_available())'
```

这里固定 `vllm==0.19.1`、`transformers==5.8.0`；PyTorch 等关联依赖由 vLLM 的依赖约束安装。不要把音频依赖安装到该环境。上面的新环境安装步骤没有在所有 Linux/CUDA 组合上验证，若官方 wheel 与本机驱动不匹配，按所选 vLLM 版本的官方安装说明选择兼容构建，再重新运行 `pip check`。单独更换 torch 后不要忽略二进制兼容问题。

先查看无副作用的启动计划：

```bash
.venv-vision/bin/python deploy/vision/serve.py --dry-run
```

实际前台启动：

```bash
CUDA_VISIBLE_DEVICES=0 .venv-vision/bin/python deploy/vision/serve.py
```

这个进程加载完整模型，首次内核编译可能需要时间。等日志显示服务就绪，再进行实际调用。停止时在该终端按 Ctrl+C。启动器不会寻找或停止其他 GPU 进程。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `VISION_MODEL_DIR` | 仓库内 `data/models/Qwen3.8-27B-FP8` | 完整模型目录 |
| `VISION_MODEL_NAME` | `Qwen3.8-27B-FP8` | API 的 served model alias；与应用 `MODEL_NAME` 一致 |
| `VISION_HOST` / `VISION_PORT` | `127.0.0.1` / `8020` | 模型监听地址与端口 |
| `VISION_API_KEY_FILE` | `data/secrets/vision_api_key.txt` | 密钥文件；默认路径相对仓库解析 |
| `VLLM_API_KEY` | 空 | 设置后优先于密钥文件；只传给服务进程 |
| `CUDA_VISIBLE_DEVICES` | `0` | 对服务可见的 GPU；不自动分配空闲卡 |
| `VISION_TENSOR_PARALLEL_SIZE` | `1` | 模型并行卡数，应匹配可见 GPU |
| `VISION_MAX_MODEL_LEN` | `32768` | 上下文上限；缩小会影响长历史输入 |
| `VISION_MAX_NUM_SEQS` | `4` | 引擎调度并发上限 |
| `VISION_GPU_MEMORY_UTILIZATION` | `0.65` | vLLM 显存预算比例 |

固定设置包括每次最多 8 张图、非量化运算用 BF16、Gated DeltaNet prefill 使用 Triton，关闭请求内容日志。`--dtype bfloat16` 不会将 FP8 权重换成一个 BF16 权重仓库。应用在请求里关闭 thinking，以减少直播延迟；这也要求上游保留对应 chat template。

模型加载默认设置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，因此必须先完成下载。CUDA/FlashInfer 的内核缓存与模型下载缓存是不同事项，首次编译或缺失内核可能仍需网络和编译工具。

若显存不足，先减少并发和上下文，根据错误检查实际需要的显存；减小预算比例不一定解决问题，因为模型和 KV cache 可能随之无法放下。视觉与音频可以使用不同 GPU；共享一张卡需要为两者留足空间并测量负载，不应默认照搬单服务的预算。

## 6. 安装和启动音频服务

音频必须使用独立环境，因为 `qwen-asr==0.0.6` 固定要求 `transformers==4.57.6`。本文使用 Transformers + SDPA 的短块识别接口，不安装 `qwen-asr[vllm]`，也不需要将音频环境升级成视觉服务的 Transformers 5。

Ubuntu/Debian 可先安装本地音频库及编译工具：

```bash
sudo apt-get install -y libsndfile1 sox build-essential
python3.11 -m venv .venv-audio
.venv-audio/bin/python -m pip install --upgrade pip
```

先安装版本匹配的 PyTorch/Torchaudio CUDA wheel。下面是 CUDA 12.6 wheel 的示例，应根据自己的 GPU 与驱动选择 [PyTorch 官方对应版本命令](https://pytorch.org/get-started/previous-versions/)；不能把该 wheel 示例当作所有新旧 GPU 都可运行的保证：

```bash
.venv-audio/bin/python -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu126
.venv-audio/bin/python -m pip install -r deploy/audio/requirements.txt
.venv-audio/bin/python -m pip check
.venv-audio/bin/python -c 'import torch, torchaudio; print(torch.__version__, torchaudio.__version__); print("CUDA:", torch.cuda.is_available()); print("BF16:", torch.cuda.is_bf16_supported())'
```

这里按上游普通方式安装 `qwen-asr`，其包依赖可能同时安装 Gradio/Flask 等演示相关包；本服务实际只启动 FastAPI。Qwen 官方说明见 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)。未安装 `flash-attn` 时，本仓库明确使用 PyTorch SDPA。

前台启动：

```bash
CUDA_VISIBLE_DEVICES=0 .venv-audio/bin/python deploy/audio/service.py
```

若有第二张 GPU，可以将该命令改为 `CUDA_VISIBLE_DEVICES=1 ...`。服务内部的 `cuda:0` 指向对它可见的第一张卡，不是强制使用物理 GPU 0。VAD 与 CAM++ 在 CPU 执行，Qwen-ASR 使用 GPU。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AUDIO_MODEL_DIR` | 仓库内 `data/models/Qwen3-ASR-0.6B` | ASR 目录 |
| `SPEAKER_MODEL_FILE` | 仓库内 `data/models/campplus/campplus_cn_common.bin` | CAM++ 权重文件 |
| `AUDIO_HOST` / `AUDIO_SERVICE_PORT` | `127.0.0.1` / `8021` | 模型监听地址与端口 |
| `AUDIO_API_KEY_FILE` | 仓库内 `data/secrets/audio_api_key.txt` | 私有密钥文件 |
| `AUDIO_API_KEY` | 空 | 优先于文件的环境变量密钥 |
| `AUDIO_STATE_DIR` | 仓库内 `data/deploy/audio` | 服务版本记录和时间统计日志 |
| `AUDIO_CPU_THREADS` | `4` | PyTorch CPU 线程数 |

显式指定的相对路径按进程当前目录解析；不确定时使用自己机器的绝对路径。默认路径全部随仓库移动。

API 的 `GET /health` 和 `POST /analyze` 都需要 `Authorization: Bearer ...`。音频输入为标准 base64 编码的 **16 kHz、单声道、16-bit PCM WAV，0.2–15 秒**。JSON 结构为 `{"audio_b64":"...","language":"Chinese"}`，省略或设 `language:null` 可自动识别语言。总请求体不超过 1.5 MB。单进程只同时处理一个推理请求，忙时返回 `429`，由主应用的短块调度处理丢弃与重试。

返回值包含转写、VAD 区域、局部时间和可用时的 192 维归一化声纹。少于 1.5 秒、语音覆盖不足、空转写、合并了多个 VAD 区域或声音稳定性检查失败时，不提供可用于匹配的声纹。时间戳是 VAD 区域边界，**没有字词强制对齐**。VAD、声纹相似度与声音稳定性检查都不证明单一说话人，也不能证明来自主播麦克风；服务不做重叠检测或源分离，`overlap` 保持 `unknown`。唱歌、视频对白、多人连麦和背景音仍需人工理解与角色绑定。

该服务记录模型版本与运行时间统计，时间日志每约 4 MiB 轮转一次；服务自身日志不保存音频、转写文本或声纹。但主应用会保留会话证据，见第 9 节。

## 7. 连接主应用与模型

### 三个进程同机

`configure_local.py` 已创建端点文件时，主应用 `.env` 保留：

```dotenv
MODEL_BASE_URL=
MODEL_NAME=Qwen3.8-27B-FP8
MODEL_API_KEY=
MODEL_ENDPOINT_FILE=data/model_endpoint.json
AUDIO_ENABLED=1
AUDIO_BASE_URL=
AUDIO_API_KEY=
AUDIO_ENDPOINT_FILE=data/audio_endpoint.json
```

应用会从端点文件读取地址和密钥文件。也可以直接把自己服务的配置写进 `.env`：

```dotenv
MODEL_BASE_URL=http://127.0.0.1:8020/v1
MODEL_NAME=Qwen3.8-27B-FP8
MODEL_API_KEY=replace-with-your-own-vision-key
AUDIO_ENABLED=1
AUDIO_BASE_URL=http://127.0.0.1:8021
AUDIO_API_KEY=replace-with-your-own-audio-key
```

不要原样使用示例密钥；从自己生成的私有文件复制。视觉地址有 `/v1`，音频地址没有。`MODEL_BASE_URL`/`AUDIO_BASE_URL` 非空时，会优先使用环境变量路径，不再从对应端点文件补齐地址或密钥；如果写了 URL 却没写对应 key，不会自动从旧端点文件获取 key。

配置读取使用 `python-dotenv`；通常已导出的 shell 环境变量优先于 `.env`。修改密钥、Cookie 或环境配置后重启主应用，以避免还在使用已创建客户端的旧配置。不要用 `source .env` 导入含 Cookie 的配置；它是 dotenv 数据文件，不是 shell 脚本。

### 应用在本机，模型在远端 GPU 主机

保持模型服务监听远端 loopback，在应用机器建立 SSH 转发：

```bash
ssh -N \
  -L 8020:127.0.0.1:8020 \
  -L 8021:127.0.0.1:8021 \
  YOUR_USER@YOUR_GPU_HOST
```

保留这个终端，主应用继续使用 `127.0.0.1:8020/v1` 与 `127.0.0.1:8021`。本机应用必须持有与远端服务相同的密钥；在两台机器各自运行密钥生成器会生成不同密钥，不能直接互用。通过你自己的安全传输方式复制远端所需密钥到本机 `data/secrets/`，或只将它们填入本机 `.env`。如已有本地端点文件，检查它们仍指向正确密钥。

两种模型运行在不同 GPU 主机时，分别建立到对应主机的一个端口转发。默认端口被占用时可以更换本地转发端口，再修改主应用 URL；不要去终止不属于本项目的进程。

### 网页在远端主机

默认主应用只监听 loopback，并没有完整的用户登录与多租户隔离。个人远程使用可转发应用端口：

```bash
ssh -N -L 7862:127.0.0.1:7862 YOUR_USER@YOUR_APP_HOST
```

再从本机浏览器访问 `http://127.0.0.1:7862`。若要提供公网网页，应自行配置 HTTPS、访问认证、允许 SSE 长连接的反向代理和访问控制。仅改成 `HOST=0.0.0.0` 不会添加这些能力。模型 bearer key 也不替代主应用的用户登录；不要把模型密钥交给前端 JavaScript。

## 8. 逐层检查，确认真正可用

### 主应用与视觉调用

主应用启动后，在另一个终端执行：

```bash
curl -fsS http://127.0.0.1:7862/api/health
curl -fsS -X POST http://127.0.0.1:7862/api/model/check
```

第一条只检查应用状态与是否有模型配置；`configured:true` 不是推理成功。第二条通过应用发出真实的图像模型检查，失败时应检查模型是否就绪、URL、密钥、served model 名和视觉支持。不要用 `/health` 成功替代图像测试。

### 音频认证和真实转写

下面使用音频服务默认密钥文件检查健康状态，不把密钥输出到终端；在模型主机或持有同样密钥且有转发的应用机器执行：

```bash
python3 - <<'PY'
import json
from pathlib import Path
from urllib.request import Request, urlopen
key = Path('data/secrets/audio_api_key.txt').read_text().strip()
req = Request('http://127.0.0.1:8021/health', headers={'Authorization': 'Bearer ' + key})
with urlopen(req, timeout=15) as response:
    print(json.load(response))
PY
```

健康状态表示模型已加载。要测试实际识别，准备一份你有权使用的、清晰且含实际人声的本地音频，先转换前 5 秒：

```bash
mkdir -p data/checks
ffmpeg -i YOUR_SPEECH_FILE -t 5 -ac 1 -ar 16000 -c:a pcm_s16le data/checks/speech.wav
```

然后向 `/analyze` 发送 WAV：

```bash
python3 - <<'PY'
import base64
import json
from pathlib import Path
from urllib.request import Request, urlopen
key = Path('data/secrets/audio_api_key.txt').read_text().strip()
payload = {'audio_b64': base64.b64encode(Path('data/checks/speech.wav').read_bytes()).decode(), 'language': None}
req = Request('http://127.0.0.1:8021/analyze', data=json.dumps(payload).encode(),
              headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
with urlopen(req, timeout=120) as response:
    result = json.load(response)
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
```

对照原音频检查文本、时间范围与声音情况。打印的转写和声纹属于你自己的测试数据，不要随意提交到仓库。静音应返回 `speech:false` 与空文本，真实短语音可能因为声纹门槛返回 `embedding:null`，这不必然是错误。

没有加载 GPU 模型的 CPU 边界检查可运行：

```bash
.venv-audio/bin/python deploy/audio/test_service.py
```

这些检查验证 WAV 格式、请求大小、认证拒绝和声音质量筛选逻辑，不能替代 ASR 准确率、声纹准确率或端到端 GPU 验证。发布整理时，这 7 项 CPU 检查通过；还检查了 Python 语法、CAM++ 文件 SHA256、下载与视觉启动的 dry-run、临时目录内的密钥权限及重复初始化，以及不执行模型进程的启动参数检查。没有因此启动新的 GPU 服务，也没有重跑全新环境安装或大模型下载。

### 直播验收

打开网页并输入自己要观察的、正在直播的房间。分别确认房间状态、播放画面、弹幕连接、连续视觉观察、音频块和问答；仅播放器有画面并不代表模型功能已连通。对照画面与实际声音检查一段观察和一次问答，确认回答里的视觉/音频证据来自相应时间窗口。

游客取流或弹幕连接被平台拒绝时，再按 [绑定自己的 Bilibili Cookie](../README.md#绑定自己的-bilibili-cookie) 配置自己的登录 Cookie。网页上的“听到什么”与“谁在说话”是不同判断；先匿名观察，再显式绑定已确认的角色。

## 9. 日常运行、数据与更新

三个服务应分别保持一个进程；主应用包含内存状态与持续任务，应保持 `--workers 1`。上述命令以前台运行，终端关闭可能结束进程。需要长期运行时，可用自己的 systemd、进程管理器或 tmux 会话包装同一启动命令，并设置正确工作目录、解释器、环境变量与日志目录。本文不提供未经验证的 Docker 镜像，也不依赖外部作业调度器。

`data/` 保存当前机器的模型、端点配置和会话输出，可能包含直播截图、转写、问答、弹幕记录、声纹及角色标签等。先停止对应会话/进程，再选择性备份或清理。删除权重会要求重新下载；删除密钥后需同时更新客户端与服务；不要将整个 `data/` 当作公开示例提交。

音频运行时状态在 `data/deploy/audio/`，启动失败信息输出到运行终端。若自行重定向 stdout/stderr，日志也留在 `data/` 等忽略目录，并实施自己的保留策略。主程序不会替你保证长期磁盘空间充足。

更新源代码后在对应环境安装变更的 requirements，并重新执行 `pip check` 和实际接口检查。模型版本更新则修改 `deploy/models.json`、下载到新目录、验证后再切换服务目录；保留旧模型时便于回退。修改视觉依赖不要影响音频环境，反之亦然。

## 10. 常见问题

| 现象 | 检查方向 |
| --- | --- |
| `No module named ...` | 是否使用正确 `.venv*/bin/python`；在那个环境安装对应 requirements |
| 找不到 FFmpeg | 系统安装、PATH、`.env` 中的 `FFMPEG_BIN`；Python 包不等于可执行文件 |
| 模型下载 401/403/超时 | 个人 Hub 访问权限、网络/代理、revision；检查目标磁盘空间 |
| 视觉不认识模型架构 | 是否混入音频 Transformers 4 环境，是否安装文档固定版本 |
| CUDA 不可用或库加载失败 | 驱动、wheel CUDA 版本、GPU 可见性；确认不是 CPU wheel |
| CUDA OOM | 权重与 KV cache 预算、并发/上下文、其他 GPU 进程；不要自动结束他人任务 |
| HTTP 401 | 服务与应用密钥是否相同；URL 环境变量会使端点文件配置不生效 |
| HTTP 404 | 视觉 base URL 应含 `/v1`；音频不含；检查端口与模型名称 |
| 音频 HTTP 422 | WAV 是否 mono/PCM16/16 kHz，长度是否 0.2–15 秒，语言名是否被模型接受 |
| 音频 HTTP 429 | 单推理请求繁忙，检查硬件与实时处理耗时；不要增加无界排队 |
| 页面正常但一直等待模型 | 未配置端点，模型未就绪或只有健康检查成功；运行真实图像/音频检查 |
| 音频文字出来但无声纹 | 短片段、合并区域、覆盖不足或稳定性筛选；先检查音频证据质量 |
| B 站播放或弹幕失败 | 房间是否在播、平台网络、游客限制、自己的 Cookie 是否过期 |
| 更改 `.env` 无效 | 检查 shell 已导出的同名变量，重启主应用；服务配置使用自己的 shell 环境 |
| SSH 转发后仍连接失败 | 转发终端是否存活、远端服务是否监听对应端口、本地端口是否被占用 |

## 11. 仓库应包含哪些文件

可发布的部署文件是 `deploy/*.py`、`deploy/*.json`、`deploy/requirements-download.txt`、`deploy/vision/*.py`、`deploy/vision/requirements.txt`、`deploy/audio/service.py`、`deploy/audio/test_service.py`、`deploy/audio/requirements.txt`，以及 `deploy/audio/vendor/` 下的源文件、上游许可和来源记录。部署文档属于 `docs/`。

不要提交 `.env`、任何真实 Cookie/token/API key、`data/`、`.venv*`、模型权重、下载缓存、编译缓存、`__pycache__`、测试运行产物、私有服务地址/元数据与运行日志。源码下载者应依据本文生成自己的环境和凭据。
