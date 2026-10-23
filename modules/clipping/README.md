# B站轮巡、自动切片与投稿

本模块是 AI同担的切片工作台，与直播陪伴功能同仓库分发。它包含关注列表轮巡、直播与弹幕采集、模型选段、手动时间轴、整段语音复听、1080P字幕与封面、网页切片库、B站投稿及按主播归档。采集和模型处理使用独立任务与数据目录，不宣称与陪伴页面共享同一份录像。

## 从仓库根目录启动

需要 Python 3.10+、FFmpeg 和 ffprobe。推荐 Linux、macOS 或 WSL；轮巡及清理工具使用 Unix 文件锁，原生 Windows 未完整验证。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python modules/clipping/scripts/setup_assets.py
cp .env.example .env  # 仅首次创建；已有配置不要覆盖
bash start.sh
```

- 直播陪伴：`http://127.0.0.1:7862/`
- 切片工作台：`http://127.0.0.1:6880/`
- 切片库：`http://127.0.0.1:6880/static/clips/index.html`

顶层启动器同时管理陪伴、切片、切片库刷新和临时录播清理进程。清理必须另行启用策略；默认不启动轮巡、不自动发弹幕或投稿。按 Ctrl+C 会停止本次启动的进程；不要对正在投稿的实例强制结束进程。仅启动原陪伴版可用 `bash start.sh --companion-only`。

也可单独启动工作台：

```bash
cd modules/clipping
../../.venv/bin/python -m clipper
```

默认仅监听本机。远程使用时分别转发 7862 和 6880；`HOST`/`PORT`配置陪伴服务，`CLIPPER_HOST`/`CLIPPER_PORT`配置切片服务。页面导航默认沿用浏览器当前主机名，反向代理可用 `CLIPPER_PUBLIC_URL`设置切片服务的公开域名与端口。工作台使用根路径部署，暂不支持 `/xx/` 子路径挂载。每个工作台进程只运行一个 worker，不要让两个进程共享切片数据库。

## 配置模型与账号

根 `.env`可被两个模块读取，模块自己的 `modules/clipping/.env`可覆盖切片设置。默认切片数据保存于 `modules/clipping/data/`，不与陪伴会话混用。可用 `CLIPPER_DATA_DIR`改成独立目录。

公共 `MODEL_*`/`AUDIO_*`端点可复用；想让切片独立用模型，填写 `CLIPPER_MODEL_BASE_URL`、`CLIPPER_MODEL_NAME`、`CLIPPER_MODEL_API_KEY`及相应 `CLIPPER_AUDIO_*`，或设置 `CLIPPER_MODEL_ENDPOINT_FILE`、`CLIPPER_AUDIO_ENDPOINT_FILE`。端点文件沿用项目的 `base_url`、`model`、`api_key_file`格式；端点文件及密钥不随源码分发。

视觉服务须支持多图与 JSON 输出。实时音频服务使用 `/analyze`；整段复听还需要 `/transcribe_clip`，返回完整文本、逐词时间戳、模型和强制对齐信息。普通聊天接口或仅有短音频接口不能替代整段复听。服务配置缺失时，可导入录像并手工编辑；自动稿件不能绕过完整字幕审查。

`BILIBILI_COOKIE_FILE`或`BILIBILI_COOKIE_JSON`用于读取直播与关注列表。投稿 Cookie 独立配置为 `BILIBILI_UPLOAD_COOKIE_FILE`，默认读取 `modules/clipping/data/private/bilibili-upload.cookie`。本机文件建议权限600；它不会自动沿用网页弹幕发送账号。

### 部署强语音服务（可选）

已有兼容服务时可跳过。自部署使用独立的 CUDA Python 环境，先按仓库[模型部署文档](../../docs/deployment.md)安装匹配的 PyTorch/torchaudio，以及 `deploy/audio/requirements.txt`。这套服务不由网页进程加载模型。

```bash
# 以下下载工具需要 deploy/requirements-download.txt 中的依赖。
python deploy/download_models.py --component clipping-audio --dry-run
python deploy/download_models.py --component clipping-audio

# 在已安装音频推理依赖的环境中，从仓库根目录启动。
AUDIO_MODEL_DIR="$PWD/data/models/Qwen3-ASR-1.7B" \
AUDIO_OFFLINE_MODEL_DIR="$PWD/data/models/Qwen3-ASR-1.7B" \
AUDIO_ALIGNER_MODEL_DIR="$PWD/data/models/Qwen3-ForcedAligner-0.6B" \
SPEAKER_MODEL_FILE="$PWD/data/models/campplus/campplus_cn_common.bin" \
AUDIO_API_KEY_FILE="$PWD/data/secrets/clipping-audio.key" \
AUDIO_SERVICE_PORT=8921 \
python modules/clipping/deploy/audio_service.py
```

密钥文件需要自己创建，客户端配置相同密钥。模型下载固定到 `deploy/models.json` 中的版本，权重不入 Git。实时1.7B与整段1.7B/ForcedAligner使用独立实例、VAD和锁，实际显存需求需按目标硬件验证；本次源码集成测试不代表对任意GPU的性能保证。

## 边看边切

在切片页面输入房间号或直播链接开始采集，也可导入录像、字幕或事件。积累约两分钟素材后，每新增约45秒检查一轮，回看最近15分钟；自动导出要求模型分数至少88、片段至少60秒、话题有可理解的起止。直播中保留后续语境，不必等主播下播。分数是选段信号，不是爆款概率。

手动可在时间轴打点、修改起止、标题和封面。自动及手动导出均使用1080P，字幕整段复听后烧录。弹幕强调按视频原帧核验位置，适合时才在原位置小幅放大；可选OCR依赖见 `requirements-ocr.txt`。默认无需每条都放大弹幕。

切片库支持状态、主播、关键词组合筛选、网页播放和下载。它区分已公开、平台审核、未通过、时长不足、批次暂停及未审稿；“未投稿”是汇总分类，不表示全部正在排队。刷新列表不会打断播放器。工作台断开时显示上次快照和更新中断提示。

## 关注列表轮巡

先启动工作台并配置自己的登录态，以下从仓库根目录执行：

```bash
.venv/bin/python modules/clipping/scripts/run_following_patrol.py inspect-following
.venv/bin/python modules/clipping/scripts/run_following_patrol.py run \
  --base-url http://127.0.0.1:6880 \
  --dwell-min 2700 --dwell-max 3720 --minimum-score 88
.venv/bin/python modules/clipping/scripts/run_following_patrol.py health
.venv/bin/python modules/clipping/scripts/run_following_patrol.py stop
```

每个房间随机停留45–62分钟，下播提前切换；换房前处理尾部语音与分析。`--until-offline-room 房间号`可指定这一场看到下播再恢复轮巡；`--next-room 房间号`可指定下一站。它不发送弹幕。不要对同一数据目录重复运行轮巡控制器。

额外模型配置放在切片数据目录的 `model-profiles/<名称>.json`，名称只用小写字母、数字、下划线和连字符。字段包含 `version:1`、`profile`、`model_endpoint`、`audio_endpoint`，可用 `owner_uid`/`room_id`限定目标。通过直播API指定该 `model_profile`及唯一 `capture_owner`，可使用独立采集通道；缺少配置时失败，不回落到主模型。源码不绑定某个主播或私有GPU平台。

## 开启自动投稿

默认关闭。先配置投稿Cookie，在工作台确认账号和分区，然后显式开启：

```bash
.venv/bin/python modules/clipping/scripts/configure_auto_publish.py status
# 把下面UID替换为你自己的账号UID；脚本会核对Cookie对应账号。
.venv/bin/python modules/clipping/scripts/configure_auto_publish.py enable --account-uid 123456
.venv/bin/python modules/clipping/scripts/configure_auto_publish.py disable
```

启用后仅纳入启用时间之后的新片；历史库存不会因开关变化被批量补投。生成片段后完整复听、独立内容审稿，通过即投稿，没有固定5–10分钟等待。必须满足1080P、至少60秒、账号一致、来源可验证、视频/封面哈希一致、不重叠不重复。原直播素材按转载处理，原标题格式包含`【主播短称】`；标题、简介及标签依据当前片段和主播身份。

校验器支持跨连续字幕原话及中文数词。旧版本因引用或数字校验被误拦的候选，经当前校验确认后最多完整重审一次；每轮每个模型配置最多一条旧稿，新片优先，保留历史报告。同版本失败不会无限重试。修复格式不等于内容合格，重叠或不合格仍会拦截。

“已提交”不等于已公开。平台状态由后台查询；提交超时或结果不明确会停止自动重投，避免重复稿件。B站活动只有核实当前资格及实际稿件条件后才选择，不会将转载伪装为原创。合集按主播身份归档，超过两条时尝试创建；账号未获合集能力时显示实际失败原因，不能把普通列表当作B站合集。

## 只保留切片

轮巡默认有磁盘预算。需要自动释放完整录播缓冲时，在切片数据目录创建 `recording-retention/policy.json`：

```json
{"version":1,"mode":"clips_only","enabled":true}
```

顶层启动器已运行清理worker。也可手工查看计划：

```bash
.venv/bin/python modules/clipping/scripts/run_recording_retention.py plan
```

只清理已停止且不再被分析/导出占用的直播源文件，保留成片、投稿与审稿记录；失败待恢复的素材默认保留。已清理的原始片段不能再任意重新选段。不要把`data/`、Cookie、录播或私人端点提交到Git。

## 开发与验证

图库已包含可运行静态构建，普通使用无需Node。修改图库后，在 `local-library/`安装锁定依赖并构建；具体命令见该目录 `package.json`。

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
(cd modules/clipping && ../../.venv/bin/python -m pytest -q)
```

测试覆盖媒体时间轴、真实FFmpeg导出、字幕证据、源时间重叠、发布恢复、误伤重审、轮巡和清理。GPU模型服务测试使用隔离替身验证协议与并发边界；不能据此认定生产模型的字幕必然准确。
