# 在自己的电脑发弹幕

「本机发送」把真正发弹幕的请求交给你的电脑：AI 同担仍负责看直播、生成候选和显示记录，B 站 Cookie 由本机助手读取。助手只把任务编号和脱敏结果交回 Demo，不把 Cookie、账号资料或私有诊断文件上传给 Demo。

这个助手不需要显卡，也不运行大模型。需要 Python 3.9 或更新版本、你本人已登录的 B 站浏览器，以及一个能打开 Demo 的本机转发端口。它使用你正常的 B 站账号权限；切换发送位置并不能保证解决所有平台拒收或展示问题。

## 1. 下载到你自己的电脑

在 AI 同担网页的弹幕发送区域，在「从哪里发弹幕」选择「我的电脑」，点击「下载本机发送助手」。把 ZIP 解压到自己电脑的文件夹，例如桌面的 `AI同担本机助手`。安装好后再生成配对码，避免安装期间配对码过期。

**下面的命令都在自己的 Windows、macOS 或 Linux 电脑执行。** 如果你使用 VS Code 远程连接服务器，远程终端仍然在服务器上；请另外打开自己电脑的命令提示符或终端。

已有 Python 3.9 或更新版本即可运行本机助手，不必为助手单独升级。尚未安装 Python 时，可从 [Python 官网](https://www.python.org/downloads/) 安装当前稳定版；Windows 安装时勾选 `Add Python to PATH`。远程 Web 应用的 Python 要求仍以仓库部署文档为准。

旧下载包可能提示「需要 Python 3.10 或更新版本」：请从 Demo 重新下载助手，解压到新文件夹，将自己的 `bilibili_cookie.txt` 复制过去后启动。不要复制旧目录中的 `.venv-local-sender`。

## 2. 安装助手并导出自己的 Cookie

先运行对应启动脚本，让它建立独立环境、安装所需依赖：

| 系统 | 启动方式 |
| --- | --- |
| Windows | 双击 `start-local-sender.bat` |
| macOS | 在终端进入解压目录，运行 `sh start-local-sender.sh`；也可双击 `start-local-sender.command` |
| Linux | 在终端进入解压目录，运行 `sh start-local-sender.sh` |

### macOS 提示「Apple 无法验证」

下载包里的 `.command` 启动脚本没有 Apple 签名或公证，因此双击时可能被 macOS 拦截。这条提示表示系统无法验证它，并不等于系统已经检测到恶意软件。

如果这个文件来自 Demo 的「下载本机发送助手」，且你确认没有被替换，可以在尝试打开后，进入 **系统设置 → 隐私与安全性**，向下找到这个脚本对应的拦截提示，点击 **仍要打开**，再确认打开。这只为这个文件添加例外，无需关闭系统的安全检查。参见 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。

也可以在 **Mac 自己的「终端」** 中进入解压目录，运行 `sh start-local-sender.sh`。不熟悉目录切换时，先输入 `cd `（末尾有一个空格），把解压后的文件夹拖进终端，再按回车；然后输入上述启动命令。不要在连接远程服务器的 VS Code 终端中运行。

启动脚本会在这个文件夹中创建 Python 虚拟环境、安装依赖，然后启动本机助手。Cookie 文件由助手在本机读取，不会上传给 Demo。

初次运行还没有 Cookie 文件时，脚本会显示导出方法。先在**这台电脑**的浏览器登录 B 站，再在解压目录运行下面命令。

Windows 的命令提示符：

```bat
.venv-local-sender\Scripts\python.exe -m pip install -U browser-cookie3
.venv-local-sender\Scripts\python.exe get_bilibili_cookie.py --browser chrome
```

macOS / Linux：

```sh
.venv-local-sender/bin/python -m pip install -U browser-cookie3
.venv-local-sender/bin/python get_bilibili_cookie.py --browser chrome
```

使用其他浏览器时，将 `chrome` 改为 `edge`、`firefox`、`safari`、`brave` 或 `chromium`。导出脚本只读取本机浏览器，不会联网。如果浏览器加密机制导致读取失败，可退出浏览器再试，或按导出脚本的 `--cookie-file` 选项指定正确的浏览器配置数据库；有些系统或浏览器版本无法通过该库导出。

如果浏览器导出库无法读取，也可以在本机浏览器打开并登录 B 站官方直播间，按 `F12` 打开开发者工具，在 `Network / 网络` 中刷新页面，选择发往 `api.bilibili.com` 或 `api.live.bilibili.com` 的已登录请求，在 `Request Headers / 请求标头` 找到 `Cookie`，只把这一项的值保存到本机解压目录的 `bilibili_cookie.txt`。不要复制整段请求，也不要把这串值粘贴到远程 Demo；保存内容仍需要包含 `SESSDATA` 和 `bili_jct`。

成功后，文件夹会出现 `bilibili_cookie.txt`。其中需要包含 `SESSDATA` 和 `bili_jct`；它相当于本机登录凭据，不要上传或分享。脚本默认不覆盖已有文件；更新时先删除旧文件，或用 `--output` 导出到新文件，再通过助手的 `--cookie-file` 指定它。

助手只在启动时读取 Cookie。更新文件后，先关闭助手，再重新运行启动脚本，并在网页生成新的配对码完成连接。

## 3. 确认 Demo 端口可以从本机访问

默认地址是 `http://127.0.0.1:7862/`，与主应用默认端口一致。先确认自己电脑的浏览器能打开这个地址。使用 VS Code 远程连接时，在「端口」面板转发服务器的 Demo 端口，并查看对应**本地端口**。

如果本地端口不是 7862，可以在启动时指定，例如：

Windows 命令提示符：

```bat
start-local-sender.bat --demo http://127.0.0.1:7863
```

macOS / Linux：

```sh
sh start-local-sender.sh --demo http://127.0.0.1:7863
```

助手只接受本机 HTTP 根地址：`127.0.0.1`、`localhost` 或 `[::1]`，不能带路径、登录参数或重定向。不要填写远程服务器域名；把它的端口转发到本机后再连接。

## 4. 配对，开始使用

1. 在 Demo 网页的「从哪里发弹幕」选择「我的电脑」，点击「连接本机助手」生成 8 位数字配对码。
2. 重新运行本机启动脚本。在终端输入配对码并回车；输入时不会显示字符。
3. 看到「本机助手已连接」后，让这个终端保持打开。
4. 在网页点击发送候选弹幕，或手动编辑后发送。普通会话中的「一键融入」也使用当前选择的发送方式，需要你另外点击启动；配对助手不会自动启动发送。

如果当前轮巡显示「仅生成候选」，助手也不会发送；显示「仅手动发送」时，只响应你在网页点击的发送操作，不能启动「一键融入」。这是当前会话的发送策略，重新配对或更新 Cookie 不会解除它。

Cookie 只在助手启动时读取。更新 `bilibili_cookie.txt` 后，请关闭助手，在网页重新生成配对码，再启动助手连接。

配对码有时效；失效就重新生成。重新配对、切换发送方式或直播间状态改变可能取消正在等待的任务。助手在每条弹幕实际发送之前重新向 Demo 确认授权；如果授权过期，这条不会发送。

按 `Ctrl+C` 或关闭助手窗口即可停止接收新任务。已经发出的网络请求可能仍在平台处理，请以官方直播间显示为准。助手离线时不会自动回退到服务器发送。

## 结果与常见情况

- **已受理**：B 站接口给出明确成功回执。是否最终展示仍请看官方直播间。
- **结果待确认**：平台给出未识别回复（包括短标记 `f`），或发出请求后网络断开。助手不会自动重发，先核对官方直播间。
- **配对已失效**：重新在网页生成配对码，再启动助手。不要把配对码或终端状态当作 B 站 Cookie。
- **网页提示助手离线**：确认本机终端还开着、本机转发地址可访问，并检查 `--demo` 端口。
- **要求重新登录或验证码**：先在官方页面处理登录或验证，再更新本机 Cookie。
- **没有弹幕显示**：先检查网页上的安全回执。成功受理与直播间实际展示是不同阶段；本机方式不绕过平台的权限或审核。

本机 `.aitongdan-local/` 保存任务编号，用于防止同一任务重复发送，还保存最近一次脱敏发送诊断。不要在任务进行中删除这个目录。这里不保存弹幕正文或 Cookie；Cookie 只从你指定的本机文件读入内存。诊断文件只在本机，助手不会上传它，也不会输出原始上游回复。

网络断开后，助手只继续领取新任务。发送结果的同步可以有限重试，同一条 B 站发送请求不会自动重试；程序重启也会保留任务去重记录。

## 不用启动脚本的方式

在自己的电脑创建 Python 虚拟环境、安装依赖后可直接运行：

```sh
python3 -m venv .venv-local-sender
.venv-local-sender/bin/python -m pip install -r requirements-local-sender.txt
.venv-local-sender/bin/python local_sender.py --demo http://127.0.0.1:7862 --cookie-file bilibili_cookie.txt
```

Windows 将 `.venv-local-sender/bin/python` 替换成 `.venv-local-sender\Scripts\python.exe`，并用 `py -3 -m venv .venv-local-sender` 创建环境。

可选参数：

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--demo` | `http://127.0.0.1:7862` | 本机转发后的 Demo 根地址 |
| `--cookie-file` | `bilibili_cookie.txt` | 自己电脑上的 Cookie 文本文件 |
| `--private-dir` | `.aitongdan-local` | 本机任务去重记录和脱敏诊断目录 |

下载包只包含固定清单的程序、脚本、说明和许可文件，不包含服务器上的登录信息、录制资料、语料 JSON 或历史数据。
