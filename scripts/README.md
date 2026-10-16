# 轮巡与本地复盘工具

这些工具在 **Linux、macOS 或 WSL** 运行；轮巡使用 Unix 文件锁和信号，不能直接在原生 Windows Python 中运行。本机发送助手是独立工具，使用方法见 [本机发送说明](../docs/local-sender.md)。

先按 [部署说明](../docs/configuration.md) 启动 AI 同担，确认本机 `http://127.0.0.1:7862/` 可打开，模型已配置；读取个人关注列表需要自己的 B 站登录配置。以下命令从仓库根目录执行，所有数据留在已忽略的 `data/patrol/`。

## 一小时、不发送的最小示例

每个房间停留 15 分钟，优先选择正在直播的关注主播；只有关注列表无人开播时才进入虚拟分区。创建一个**新的**运行目录，避免覆盖已有计划：

```bash
mkdir -p data/patrol
mkdir data/patrol/example-run
cat > data/patrol/example-run/plan.json <<'JSON'
{
  "rooms": [],
  "questions_enabled": true,
  "question_interval_seconds": 165
}
JSON
.venv/bin/python scripts/patrol_following.py run \
  --run-dir data/patrol/example-run \
  --base-url http://127.0.0.1:7862 \
  --hours 1 --dwell 900 --candidate-count 5 --virtual-fallback
```

没有 `--send` 时不自动发弹幕，默认也不允许手动发送。希望保留网页手动点击发送，可额外加 `--allow-manual-send`；这仍不会允许轮巡自动发送。自动发送属于另一个显式选项 `--send`，不能与 `--allow-manual-send` 同时使用，上例没有启用它。

`plan.json` 启用均匀候选采样及计划提问：每次停留默认均匀安排 **10 轮，每轮保存五条候选和当时全部“大家在刷”**。候选过期或未就绪会记录跳过，不会用旧候选补数。`questions_enabled: true` 才启用计划问题，目标间隔可设为 150–180 秒，实际还受新鲜证据与模型忙闲限制。手动提问额外记录，不替换计划问题。没有计划文件时仍会采集事件，但不会自动启用这些均匀模拟轮次和计划问题。

`rooms` 留空会从自己的关注列表选房；需要预排房间时，每个条目包含唯一 `plan_id`、自己的目标 `room_id`，可加 `dwell_seconds`（60–3600）和 `simulation_rounds_per_visit`（1–60）。该计划只用于不自动发送模式。当前提问器保留一个实验规则：房间 510 在停留 20 分钟后，条件满足时追加问歌名；其他房间按现场内容提问。

轮巡与网页共用一个当前会话。手动切房会让轮巡让出控制；正常手动提问可继续使用。不要同时运行多个轮巡进程。

## 查看、停止与恢复

```bash
.venv/bin/python scripts/patrol_following.py status --run-dir data/patrol/example-run
.venv/bin/python scripts/patrol_following.py stop --run-dir data/patrol/example-run
```

`stop` 请求保存并结束。意外进程中断后可使用 `resume --run-dir ...` 接续仍在原截止时间内的不自动发送任务；它不会延长截止时间，也不会恢复已被手动切房接管的任务。主动停止产生的 `STOP` 文件会阻止继续执行，开启新一轮请使用新目录。

`status.json` 保存总体进度；`visits/` 下保存每房的事件、候选、均匀轮次、问答与录屏。轮巡过程会更新复盘报告，也可停止后离线重建：

```bash
.venv/bin/python scripts/patrol_report.py data/patrol/example-run
.venv/bin/python scripts/patrol_review_media.py \
  --run-dir data/patrol/example-run --output static/patrol-review
.venv/bin/python scripts/build_patrol_review.py \
  --run-dir data/patrol/example-run --output static/patrol-review
```

在 Demo 打开 `/static/patrol-review/index.html` 查看本地快照。媒体导出只选已关闭的录屏片段，原片使用硬链接，因此输出目录需与原片处于同一文件系统。复盘页、录屏与原始记录均是个人数据，不应提交仓库；源码分发不包含这些文件。

录像需要 FFmpeg，媒体复盘还需要 ffprobe。程序优先使用 `FFMPEG_BIN` / `FFPROBE_BIN` 指定的可执行文件，再从 PATH 查找。例如：

```bash
export FFMPEG_BIN=/your/tools/ffmpeg
export FFPROBE_BIN=/your/tools/ffprobe
```

把示例路径替换为自己的真实路径；系统已安装并加入 PATH 时无需设置。`patrol_review_media.py` 不读取 `.env`，单独使用时应在 shell 中导出这些变量。
