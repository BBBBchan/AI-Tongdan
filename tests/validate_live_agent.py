#!/usr/bin/env python3
"""Real endpoint integration check using explicitly synthetic visual fixtures.

Run with `.venv/bin/python tests/validate_live_agent.py`. This is deliberately
not a pytest test: it waits for the deployed endpoint and consumes real GPU
inference. No Bilibili content is used or claimed in this validation.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import LiveAgent
from app.model import VisionModel


def fixtures(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    font = ImageFont.truetype(str(font_path), 30) if font_path.exists() else ImageFont.load_default()
    scenes = [
        ("A: RED BALL ON LEFT", "red", "ball", 240),
        ("B: RED BALL ON RIGHT", "red", "ball", 720),
        ("C: BLUE BOX ON RIGHT", "blue", "box", 720),
        ("D: BLUE BOX ON LEFT", "blue", "box", 240),
    ]
    paths = []
    for index, (title, color, shape, x) in enumerate(scenes):
        canvas = Image.new("RGB", (960, 540), "white")
        drawing = ImageDraw.Draw(canvas)
        drawing.text((35, 35), title, fill="black", font=font)
        drawing.line((480, 120, 480, 475), fill="gray", width=3)
        drawing.text((70, 485), "LEFT", fill="black", font=font)
        drawing.text((760, 485), "RIGHT", fill="black", font=font)
        bounds = (x - 95, 180, x + 95, 370)
        if shape == "ball":
            drawing.ellipse(bounds, fill=color)
        else:
            drawing.rectangle(bounds, fill=color)
        path = directory / f"scene_{index + 1}.jpg"
        canvas.save(path, quality=90)
        paths.append(path)
    return paths


async def deployed_model(endpoint_path: Path, wait_seconds: float) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            value = json.loads(endpoint_path.read_text())
            base = value.get("base_url", value.get("api_base_url", "")).rstrip("/")
            key_path = value.get("api_key_file")
            key_file = Path(key_path) if key_path else None
            if key_file is not None and not key_file.is_absolute():
                key_file = endpoint_path.parent / key_file
            key = key_file.read_text().strip() if key_file else os.getenv("MODEL_API_KEY", "")
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            if base:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    response = await client.get(base + "/models", headers=headers)
                    response.raise_for_status()
                return {"base_url": base, "model": value["model"], "api_key": key, "timeout": 150}
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("部署端点在等待时间内未就绪；没有运行或伪造模型验证。")
        await asyncio.sleep(3)


async def wait_for(predicate, timeout=240):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("真实模型集成验证等待超时。")
        await asyncio.sleep(0.1)


async def validate(args) -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or ROOT / "test-results" / "live-agent" / timestamp).resolve()
    output.mkdir(parents=True, exist_ok=True)
    print(f"Waiting for deployed endpoint; results directory: {output}", flush=True)
    report = {"fixture_type": "synthetic_images_not_live_broadcast", "started_at": timestamp,
              "observation_mode": "four_frame_async_stride3_concurrency2",
              "status": "waiting_endpoint", "questions": {}, "events": [], "checks": {}}
    report_file = output / "result.json"
    def save():
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    try:
        config = await deployed_model(args.endpoint, args.wait_seconds)
    except Exception as exc:
        report.update(status="endpoint_unavailable", error=str(exc))
        save()
        print(report["error"], flush=True)
        return 2
    model = VisionModel(**config)
    report["model"] = config["model"]
    report["status"] = "running"
    inputs = fixtures(output / "fixtures")
    started = time.monotonic()
    observations_during_question = []
    timings = {}
    agent = None
    feeder = None
    peak_inflight = 0

    async def emit(kind, data):
        nonlocal peak_inflight
        now = time.monotonic() - started
        report["events"].append({"elapsed": round(now, 3), "type": kind, "data": data})
        if kind == "observer":
            peak_inflight = max(peak_inflight, data.get("inflight_observations", 0))
        if kind == "observation":
            pending = [qid for qid in timings if agent._assistant_message(qid)
                       and agent._assistant_message(qid)["status"] in {"queued", "planning", "answering"}]
            if pending:
                observations_during_question.append({"observation_id": data["id"], "pending_questions": pending})
            print(f"observation={data['id']} delta_changed={data['changed']} pending_questions={len(pending)}", flush=True)
        elif kind == "answer_done":
            qid = data["question_id"]
            report["questions"][qid] = {**data, "latency_seconds": round(now - timings.get(qid, now), 3)}
            print(f"question_status={data['status']} latency={report['questions'][qid]['latency_seconds']}s", flush=True)
        elif kind == "error":
            print(f"agent_error[{data.get('source')}]: {data.get('message')}", flush=True)
        save()

    agent = LiveAgent("synthetic-integration", output / "agent", model, emit,
                      max_frames=24, max_observations=30, observation_frames=4,
                      observation_stride=3, observation_concurrency=2)
    try:
        await agent.start()
        # Same first/last frame, with a genuine intermediate move to the right.
        # A first-vs-last-only observer cannot recover this transient change.
        for index, fixture_index in enumerate((0, 1, 0, 0)):
            await agent.offer_frame(inputs[fixture_index], index * 0.1)
        await wait_for(lambda: agent._observed >= 1)
        submission_snapshot = agent.frames[-1].timestamp

        async def feed():
            index = 1
            while True:
                await agent.offer_frame(inputs[index % len(inputs)], time.monotonic() - started)
                index += 1
                await asyncio.sleep(args.frame_interval)

        # Queue the follow-up before later synthetic frames arrive. Its visual
        # evidence must stay at submission time but dialogue may use Q1's answer.
        q1 = agent.ask("画面中的主要物体是什么颜色、什么形状，位于左边还是右边？请根据当前快照回答。")
        timings[q1] = time.monotonic() - started
        q2 = agent.ask("你刚才说的那个物体在画面的哪一侧？这次提问时的画面与第一次相同吗？")
        timings[q2] = time.monotonic() - started
        feeder = asyncio.create_task(feed())
        await wait_for(lambda: all(agent._assistant_message(q)["status"] in {"complete", "error"} for q in (q1, q2)))
        await wait_for(lambda: agent._observed >= 3)
        q3 = agent.ask("对比最早的画面与现在，物体的位置或颜色发生了什么变化？给出能确认的变化和采样时间。")
        timings[q3] = time.monotonic() - started
        await wait_for(lambda: agent._assistant_message(q3)["status"] in {"complete", "error"})
        cancelled = agent.ask("请继续检查当前快照，并详细描述物体位置。")
        timings[cancelled] = time.monotonic() - started
        await asyncio.sleep(0.4)
        await agent.cancel_question(cancelled)
        after_cancel = agent._observed
        await wait_for(lambda: agent._observed > after_cancel)
        final = agent.snapshot()
        report["observations"] = final["observations"]
        report["memory"] = final["memory"]
        report["observations_during_question"] = observations_during_question
        report["observer"] = final["observer"]
        report["peak_inflight_observations"] = peak_inflight
        completed = [agent._assistant_message(q) for q in (q1, q2, q3)]
        report["checks"] = {
            "at_least_three_real_observations": agent._observed >= 3,
            "multiple_real_visual_deltas": sum(o["changed"] for o in agent.observations) >= 2,
            "three_questions_completed": all(m["status"] == "complete" for m in completed),
            "questions_have_real_replay_evidence": all(m.get("evidence") for m in completed),
            "observer_completed_during_pending_question": bool(observations_during_question),
            "queued_question_kept_submission_snapshot": agent._assistant_message(q2).get("snapshot_time") == submission_snapshot,
            "question_cancelled": agent._assistant_message(cancelled)["status"] == "cancelled",
            "observer_continued_after_cancel": agent._observed > after_cancel,
            "all_windows_have_four_real_frames": all(o.get("frame_count") == 4 for o in agent.observations),
            "windows_committed_in_time_order": all(a["window_end"] < b["window_end"] for a, b in zip(final["observations"], final["observations"][1:])),
            "two_observations_in_flight": peak_inflight >= 2,
            "intermediate_change_identified": bool(final["observations"][0]["changed"] and "右" in final["observations"][0]["delta"]),
        }
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["observation_count"] = agent._observed
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report.update(status="failed", error=str(exc), observation_count=agent._observed)
        report["observations"] = agent.snapshot()["observations"]
    finally:
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        await agent.stop()
        await model.close()
        save()
    print(json.dumps({"status": report["status"], "checks": report["checks"], "result": str(report_file)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", type=Path, default=ROOT / "data" / "model_endpoint.json")
    parser.add_argument("--wait-seconds", type=float, default=1200)
    parser.add_argument("--frame-interval", type=float, default=1)
    parser.add_argument("--output", type=Path)
    return asyncio.run(validate(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
