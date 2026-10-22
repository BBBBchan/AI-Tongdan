<div align="center">

<a href="https://space.bilibili.com/3747570738268616"><img src="docs/assets/xiaodan/wave.png" width="240" alt="小担向你招手：黑双马尾、红瞳，戴着红黑耳机的 AI 同担" /></a>

# AI同担 · 小担

**你知道吗，你的同担可能是 AI。**

一起看直播，一起接梗，把舍不得划走的那一幕剪下来。<br>
前排给你留好啦，小担今天也在认真上班！

[![Bilibili](https://img.shields.io/badge/Bilibili-%E5%B0%8F%E6%8B%85%E7%9A%84%E4%B8%BB%E9%A1%B5-00A1D6?logo=bilibili&logoColor=white)](https://space.bilibili.com/3747570738268616)
[![Video](https://img.shields.io/badge/Video-%E5%8A%9F%E8%83%BD%E6%BC%94%E7%A4%BA-F56B8A?logo=bilibili&logoColor=white)](https://www.bilibili.com/video/BV1GqJD6UE9e)
[![DeltaStreamer](https://img.shields.io/badge/Based_on-DeltaStreamer-7464C4)](https://github.com/BBBBchan/DeltaStreamer)
[![License](https://img.shields.io/badge/License-CC_BY--NC_4.0-59A99B)](LICENSE)

[小担能做什么](#features) · [去 B 站串门](#bilibili) · [功能更新进度](#release-status) · [快速启动](#快速启动) · [模型部署](docs/deployment.md)

</div>

嗨，我是**小担**，一个基于 [DeltaStreamer](https://github.com/BBBBchan/DeltaStreamer) 的 AI 直播搭子。看直播时，我会一起看画面、听声音、追弹幕，把发生的事情整理成记忆；你可以随时问我，也可以让我帮忙接话。遇到有意思的片段，我还能继续做切片、配字幕、准备封面和投稿。

> **本次先更新功能介绍。** 当前仓库提供可部署的 **B 站直播陪伴版**；**抖音陪伴、自动切片及相关制作工具**已在独立模块中实现，代码将后续整理更新。下方会标明各模块的发布进度，快速启动对应当前 B 站版。

<a id="features"></a>

## 小担能陪你做什么

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/assets/xiaodan/wave.png" width="130" alt="招手的小担" /><br><strong>📺 陪你看</strong><br>看画面、听声音，记住直播内容。</td>
    <td align="center" width="33%"><img src="docs/assets/xiaodan/smug.png" width="130" alt="眨眼接梗的小担" /><br><strong>💬 一起聊</strong><br>随时问、接着问，弹幕也有新灵感。</td>
    <td align="center" width="33%"><img src="docs/assets/xiaodan/focused.png" width="130" alt="专注剪辑的小担" /><br><strong>✂️ 留下来</strong><br>找片段、剪视频，把喜欢的时刻带走。</td>
  </tr>
</table>

### 📺 走神了？我给你补课。

B 站和抖音的陪伴模块都围绕同一件事展开：**陪你跟上正在发生的直播。** 粘贴直播间地址，就能在网页里看直播、追弹幕，查看小担持续更新的观察和记忆。

- **画面、声音、弹幕一起看。** 场景描述、画面变化、语音转写和聊天内容共同提供上下文。
- **可以插话，也可以追问。** “刚才为什么大家都在笑？”“再说具体一点？”聊天时，观察仍会继续。
- **记不清，就翻翻记录。** 在保留范围内查看观察记录、回听音频；也能标记、改名或合并说话人，让后续问答有更多依据。
- **房间换了，记录还在。** 轮巡工具可以依次观察直播间，保存候选弹幕、问答和录像，方便之后复盘。

### 💬 接不上梗？给你五条灵感。

小担参考直播内容和最近的聊天，更新**五条可编辑的弹幕候选**。挑一句、改一改，合拍了再发出去；也会参考自己本轮发过的话，减少重复。

| 想怎么聊 | 小担怎么接 |
| --- | --- |
| 今天当个乖巧观众 | 拖动 **「纯良 ↔ 开串」**，调整接话语气；内置风格直接可用，也能参考自己的本地语料。 |
| 大家都在刷这句 | **「大家在刷」** 单独展示多人复读的原句，不占五条 AI 候选；还能抽取近两秒收到的一条弹幕填入输入框。 |
| 想让同担也聊两句 | **「一键融入」** 由你设置条数和间隔，运行中随时可停。 |
| 模型在服务器，自己在电脑前 | 配对**本机发送助手**，使用自己的网络和登录态发送；电脑不用再装模型。 |

[弹幕与风格配置](docs/configuration.md#danmaku) · [本机发送助手](docs/local-sender.md)

### ✂️ 主播下播了？我还在剪。

切片模块面向 **B 站直播和导入录像**。它结合语音、画面和观众反应寻找值得保留的片段，给出理由与时间范围，再把候选交到可以继续编辑的工作台里。

1. **先听完整，再找片段。** 记录直播画面、声音和弹幕，结合前后语境识别趣事、游戏事件、互动或吐槽，尽量保留话题的来龙去脉。
2. **哪里开场，哪里收尾，你能改。** 在时间轴上打点、调整进出点，修改标题和封面；需要时突出画面里的关键弹幕，或手动选择局部放大。
3. **横屏竖屏，都准备好。** 生成带字幕的 1080P 视频、封面和导出包，保留来源时间与片段信息；切片库支持按主播查找、预览和下载。
4. **从成片走到投稿。** 检查标题、简介、标签和来源后提交 B 站，也支持按预设开启成片后自动投稿，并跟踪平台处理状态。

轮巡时可以接着观察正在开播的关注房间，持续积累候选与成片。自动选段和字幕仍可能需要人工校对；目前主要剪出完整的连续片段。

### 📓 下班之前，还想和你聊两句。

小担也有自己的介绍片和 **「小担下班日记」** 制作流程：从当天的直播经历里挑一个有记录的故事，配上角色旁白、表情、字幕和视频。评论区的问题也可以整理成回复草稿，确认好了再接着聊。

这些制作工具会随后续代码一起整理。现在可以先去 B 站看看小担的介绍和切片，偶尔检查一下我有没有认真上班。

<a id="bilibili"></a>

## 来 B 站找小担

**[小担的 B 站主页 →](https://space.bilibili.com/3747570738268616)**

[和小担认识一下](https://www.bilibili.com/video/BV1p6nA6NEct/) · [看看 AI 同担怎么陪你看直播](https://www.bilibili.com/video/BV1GqJD6UE9e)

> “说好只看一会儿，怎么又攒了一堆要剪的片段……”

<a id="release-status"></a>

## 功能更新进度

| 模块 | 包含什么 | 当前仓库 |
| --- | --- | --- |
| **B 站直播陪伴** | 直播与弹幕、持续观察、记忆问答、声音标记、弹幕候选、本机发送与轮巡复盘 | ✅ 已提供源码与部署文档 |
| **抖音直播陪伴** | 抖音直播接入、聊天同步、连续观察与问答、候选弹幕和本人账号互动 | 🧩 代码后续更新 |
| **B 站自动切片** | 直播采集、片段发现、时间轴编辑、字幕封面、导出、投稿与切片库 | 🧩 代码后续更新 |
| **小担内容制作** | 介绍片、下班日记、角色旁白与评论回复草稿 | 🧩 制作工具后续更新 |

欢迎 Star 关注更新，也欢迎在 [Issues](https://github.com/BBBBchan/AI-Tongdan/issues) 留下想和小担一起玩的新点子。

<a id="先看看同担能陪你做什么"></a>

## 网页里长什么样

下面是当前 B 站陪伴版的交互演示。GIF 使用真实应用界面与示例数据，直播区域为自制画面；保留录制时的界面，不代表实时推理速度。当前版本提供五条候选和“一键融入”控制。[演示说明](docs/assets/README.md)

<details>
<summary><strong>📺 一起看直播：画面、弹幕和观察同步查看</strong></summary>

![一起看直播：画面、弹幕与持续更新的观察，示例数据演示](docs/assets/demo-watch.gif)

</details>

<details>
<summary><strong>💬 随时问问：连续追问，观察继续</strong></summary>

![随时问问：提问、回答与继续追问，示例数据演示](docs/assets/demo-ask.gif)

</details>

<details>
<summary><strong>✨ 参与直播间：选一句、改一改，再发送</strong></summary>

![参与直播间：选择候选、编辑草稿与复制新弹幕，示例数据演示](docs/assets/demo-danmaku.gif)

</details>

## 快速启动

以下步骤部署当前仓库中的 **B 站直播陪伴版**。

第一次接触命令行也没关系：**选自己的系统，逐段复制命令，按回车执行即可。** 下面安装的是网页应用；AI 功能需要接入模型服务，仓库不会自动附送模型或 API 额度。

- **已有兼容模型服务：** 填好地址、模型名和密钥，即可使用观察、问答和候选弹幕。本机无需 GPU。
- **还没有模型服务：** 可以先打开页面、看直播和弹幕；需要 AI 时，按 [模型部署指南](docs/deployment.md) 准备服务。默认视觉模型为 Qwen3.8-27B-FP8，自部署需要合适的 NVIDIA GPU。
- **希望同担也能听懂声音：** 还需 [配置独立音频服务](docs/configuration.md#音频模型)。播放器的直播原声不依赖它。

### 1. 准备工具（只做一次）

需要 Python **3.10 或以上**、FFmpeg，以及用于下载和更新代码的 Git。展开自己系统的说明：

<details>
<summary><strong>Windows：使用 WSL Ubuntu</strong></summary>

在开始菜单搜索 **PowerShell**，右键“以管理员身份运行”，输入：

```powershell
wsl --install
```

按提示重启电脑，从开始菜单打开 **Ubuntu**，首次进入时设置用户名和密码。之后的命令都在 Ubuntu 窗口里执行。具体系统要求与安装问题见 [微软 WSL 安装说明](https://learn.microsoft.com/zh-cn/windows/wsl/install)。

接着执行下方 Linux 的安装命令；后续步骤也继续使用 Ubuntu。原生 Windows 运行尚未完整验证。

</details>

<details>
<summary><strong>Linux / WSL Ubuntu：安装依赖</strong></summary>

在 Ubuntu 22.04 或更新版本的终端中执行：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git nano
```

输入系统密码时终端不显示字符，这是正常现象。其他发行版请按 [详细安装说明](docs/configuration.md#安装并启动应用) 准备对应工具。

</details>

<details>
<summary><strong>macOS：安装依赖</strong></summary>

打开“终端”。如果还没有 Homebrew，先按 [Homebrew 官网](https://brew.sh/zh-cn/) 完成安装，并照安装结束时的提示配置终端。然后执行：

```bash
brew install python ffmpeg git
```

</details>

执行 `python3 --version`，确认显示 3.10 或更新版本。`ffmpeg -version` 和 `ffprobe -version` 应能显示版本信息。

### 2. 下载 AI同担

下面的 HTTPS 下载无需 GitHub 登录、访问令牌或 GitHub CLI。在终端执行：

```bash
git clone https://github.com/BBBBchan/AI-Tongdan.git
cd AI-Tongdan
```

`cd` 的意思是进入文件夹。**后面的命令都在这个文件夹内执行**，不要每一步都重新打开终端。也可以打开 [仓库首页](https://github.com/BBBBchan/AI-Tongdan)，选择 **Code → Download ZIP**，解压后进入含有 `start.sh` 的文件夹；ZIP 下载也无需登录。[其他下载方式](docs/configuration.md#download)

### 3. 填好配置

首次安装时执行下面两行；已有 `.env` 就直接编辑，不要再复制覆盖：

```bash
cp .env.example .env
nano .env
```

`.env` 是保存设置的文本文件。使用键盘方向键移动，在文件里找到下面几项，改成**你实际拥有的兼容视觉服务**信息。地址来自服务提供者；不要把 B 站网址填在这里。

```dotenv
MODEL_BASE_URL=http://127.0.0.1:8020/v1
MODEL_NAME=Qwen3.8-27B-FP8
MODEL_API_KEY=替换成你自己的视觉服务密钥
AUDIO_ENABLED=0
```

上面的地址仅适用于**已经在本机启动**的模型服务，复制配置不会启动模型。其他服务要替换地址和实际模型名；API 需支持多图输入等要求，见 [模型接口配置](docs/configuration.md#接入已有模型-api)。暂时只看直播时，保留 `MODEL_BASE_URL` 和 `MODEL_API_KEY` 为空即可。

`AUDIO_ENABLED=0` 表示先关闭后台语音识别，**直播原声仍然可以播放**。已有音频服务时，按 [音频配置](docs/configuration.md#音频模型) 开启并填写其地址和密钥。

编辑完按 **Ctrl+O**，再按 **回车**保存，然后按 **Ctrl+X**退出（Mac 这里也是 Control 键）。后续修改配置后，需要重新启动应用。

### 4. 启动，和同担见面

```bash
bash start.sh
```

第一次运行会自动创建独立的 Python 环境并安装依赖，耗时取决于网络。看到类似 `Uvicorn running on http://127.0.0.1:7862` 后，**保持终端开着**，在同一台电脑的浏览器打开：

**http://127.0.0.1:7862**

粘贴一个正在开播的直播间地址，例如 `https://live.bilibili.com/732`，点击连接。播放器默认静音，想听直播声音时点击音量控件。示例房间不保证一直开播，可以换成你正在看的房间。

已配置视觉模型时，点击右上角的模型状态按钮检查连接；出现观察后试着问：“刚才发生了什么？”想发弹幕，再到“参与直播间 → 配置本人登录”绑定自己的 Cookie：[照着做的绑定教程](docs/configuration.md#绑定自己的-bilibili-cookie)。

停止时在终端按 **Ctrl+C**；下次进入同一个文件夹，再运行 `bash start.sh`。如果上次依赖安装中断，先执行 `.venv/bin/python -m pip install -r requirements.txt`，再启动。远程服务器访问见 [远程部署](docs/configuration.md#远程机器部署与访问)。

Cookie、API 密钥、本地语料 JSON、会话与直播素材不随源码分发，也不要提交到自己的公开仓库。使用远程模型时，理解所需的画面、转写、问题和少量语料参考会发给你配置的模型服务；Cookie 不会发给模型。[数据与隐私说明](docs/configuration.md#privacy)

## 需要时再翻的小手册

| 想做什么 | 去哪里看 |
| --- | --- |
| 调整模型 API、音频、端口和其他参数 | [详细配置与使用指南](docs/configuration.md) |
| 下载权重、部署视觉与音频模型 | [模型部署指南](docs/deployment.md) |
| 导出、绑定、更新或清除自己的 B 站 Cookie | [本地导出脚本](get_bilibili_cookie.py) · [Cookie 绑定教程](docs/configuration.md#绑定自己的-bilibili-cookie) |
| 远程运行 AI，用自己电脑的网络发送弹幕 | [本机发送助手：下载、配对与启动](docs/local-sender.md) |
| 标记主播声音、改名或合并说话人 | [声音与记忆](docs/configuration.md#声音说话人和记忆) |
| 按关注列表轮巡，保存候选、问答与本地复盘 | [轮巡工具与不发送示例](scripts/README.md) |
| 备份数据、更新应用、处理运行问题 | [配置指南](docs/configuration.md#数据保存恢复与清理) · [常见问题](docs/configuration.md#常见问题) |
| 使用小担形象、Logo 或了解 GIF 录制方式 | [品牌资产](docs/branding.md) · [演示素材](docs/assets/README.md) |

## 来源与许可

维护者：**[BBBBchan](https://github.com/BBBBchan)**。欢迎在 [B 站](https://space.bilibili.com/3747570738268616) 关注小担的日常。

**本项目来源于 [DeltaStreamer](https://github.com/BBBBchan/DeltaStreamer)**，在其记忆核心上扩展了直播观看、声音理解、持续问答与弹幕互动。上游代码与[论文](https://arxiv.org/abs/2610.19849)均已公开；本仓库已包含运行所需的核心文件，无需另外克隆。来源记录见 [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)。

原创代码与文档采用 **[CC BY-NC 4.0](LICENSE)，不允许商用**。第三方代码与模型保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

这是面向非商业使用的源代码公开；由于保留非商业限制，CC BY-NC 4.0 不属于 [OSI 定义](https://opensource.org/osd)下的开源许可证。

## Citation

如果本项目对你的研究有帮助，请引用其基础工作 **[DeltaStreamer](https://arxiv.org/abs/2610.19849)**。论文作者署名独立于本应用的维护者信息。

```bibtex
@article{sun2026deltastreamer,
  title  = {DeltaStreamer: Towards Human-Like Agentic Streaming Video Understanding},
  author = {Boyuan Sun and Deshui Miao and Wenzhao Gao and Shaoyong Jia and
            Shaohui Jiao and Chi Liu and Yan Shu and Derek Li and Bryan Dai and
            Shengsheng Qian and Qibin Hou},
  journal = {arXiv preprint arXiv:2610.19849},
  year   = {2026},
  url    = {https://arxiv.org/abs/2610.19849}
}
```
