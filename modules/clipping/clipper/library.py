"""Refresh the local static gallery, without initializing Store or a server."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile

import httpx

from clipper.library_manifest import ManifestError, build_manifest, counts, read_json, safe_file

ROOT = Path(__file__).resolve().parents[1]
LOCAL_FILES = ("clip.mp4", "cover.jpg", "subtitles.srt")
PUBLICATION_STATES = {"published", "submitted", "transcoding", "reviewing", "queued",
                      "uploading", "submitting", "uncertain", "failed", "rejected", "private"}
REVIEW_REASONS = {
    "source_overlap": "与已投稿或待投稿片段的源时间重叠，需要重新选段。",
    "editorial_review_requires_changes": "内容、标题或简介需修改后重新审核。",
    "editorial_evidence_invalid": "标题或内容的证据核验未通过。",
    "editorial_numeric_claim_without_evidence": "数字表述缺少足够证据。",
    "editorial_copy_invalid": "标题或简介未通过内容检查。",
    "editorial_title_too_long": "标题过长，需要修改。",
    "editorial_category_invalid": "投稿分区需要重新核对。",
    "editorial_tags_invalid": "投稿标签需要修改。",
    "editorial_tag_without_evidence": "部分标签缺少内容依据。",
    "strong_asr_not_applied": "完整语音复听尚未通过。",
    "no_aligned_speech": "缺少有效的语音时间对齐。",
    "aligned_captions_missing": "缺少完成时间对齐的字幕。",
    "caption_text_coverage_mismatch": "字幕与完整语音文本不一致。",
    "caption_timing_invalid": "字幕时间需要重新核对。",
    "caption_repetition_compressed": "重复语音的字幕时间需要核对。",
    "original_comparison_incomplete": "语音复听对比尚未完成。",
    "asr_speech_coverage_gap": "语音识别可能漏掉部分人声。",
    "asr_tail_missing": "语音识别的结尾不完整。",
    "review_audio_duration_mismatch": "提取音轨与选段时长不一致。",
    "media_not_publishable": "视频时长、画质或音轨未满足投稿要求。",
    "editorial_model_unavailable": "自动审稿服务暂不可用。",
    "editorial_review_truncated": "自动审稿结果不完整。",
    "publication_account_changed": "投稿账号发生变化，需要核对。",
    "live_media_changed_during_review": "审核期间成片发生变化，需要重新审核。",
}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _publication_records(snapshot):
    """Keep platform progress without exposing upstream errors or private URLs."""
    selected = {}
    for row in snapshot.get("publications", []):
        if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                or row["status"] not in PUBLICATION_STATES):
            continue
        status, bvid = row["status"], row.get("bvid", "")
        valid_bvid = isinstance(bvid, str) and re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid)
        if status == "published" and not valid_bvid:
            continue
        # A failed retry cannot hide a confirmed public copy of the same job.
        priority = (status == "published", max(_number(row.get(key)) for key in
                    ("checked_at", "submitted_at", "created_at")))
        previous = selected.get(row.get("job_id"))
        if previous is None or priority >= previous[0]:
            selected[row.get("job_id")] = (priority, {
                "status": status, "bvid": bvid if valid_bvid else "",
                "url": f"https://www.bilibili.com/video/{bvid}/" if valid_bvid else "",
            })
    return {key: value[1] for key, value in selected.items()}


def _review_record(job):
    current = job.get("live_publication_review")
    current = current if isinstance(current, dict) else {}
    state = current.get("status")
    state = state if isinstance(state, str) else None
    if state in {"ready", "approved", "queued", "submitted"}:
        return {"status": "approved", "label": "自动审稿通过", "source": "automatic"}
    if state in {"held", "failed"}:
        reason = current.get("reason")
        code = reason if isinstance(reason, str) and reason in REVIEW_REASONS else "review_check_failed"
        return {"status": state, "label": "自动审稿拦截" if state == "held" else "自动审稿失败",
                "source": "automatic", "reason_code": code}
    if state == "reviewing":
        return {"status": "reviewing", "label": "自动审稿中", "source": "automatic"}
    legacy = job.get("editorial_review")
    if isinstance(legacy, dict) and legacy.get("publication_allowed") is True:
        return {"status": "approved", "label": "旧版自动审稿通过", "source": "legacy_auto"}
    legacy = job.get("quality_review")
    if isinstance(legacy, dict) and legacy.get("publish_ready") is True:
        return {"status": "approved", "label": "历史审核通过", "source": "legacy"}
    return {"status": "not_reviewed", "label": "未记录自动审稿", "source": "none"}


def _paused_batch_jobs(data_dir):
    """Read only known adjacent batch files; never follow plan-supplied paths."""
    root = data_dir / "publication-batches"
    if root.is_symlink() or not root.is_dir():
        return set()
    paused = set()
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        files = [directory / name for name in ("state.json", "plan.json")]
        try:
            if any(path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024 for path in files):
                continue
            state, plan = (read_json(path, {}) for path in files)
        except OSError:
            continue
        if (not isinstance(state, dict) or not isinstance(plan, dict)
                or state.get("status") != "halted"
                or state.get("halt_reason") != "媒体已变更，需要重新确认后另建计划"
                or state.get("id") != plan.get("id") or state.get("id") != directory.name
                or not isinstance(plan.get("items"), list)):
            continue
        records = state.get("records")
        records = records if isinstance(records, dict) else {}
        for item in plan["items"]:
            if not isinstance(item, dict):
                continue
            job_id = item.get("job_id")
            if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
                continue
            record = records.get(job_id)
            record = record if isinstance(record, dict) else {}
            if record.get("status") not in {"submitted", "published", "private", "uncertain"}:
                paused.add(job_id)
    return paused


def _display_status(clip, job, session, policy, publication, batch_paused=False):
    """Project existing facts only. This never authorizes or retries a post."""
    review = _review_record(job)
    clip.update(review=review, publication=publication, warning="")
    submitted = publication.get("status") if publication else None
    if submitted == "published":
        status, label = "published", "已公开"
        detail = ("已通过自动审稿，B站稿件已公开。" if review["source"] == "automatic" and review["status"] == "approved"
                  else "B站稿件已公开。")
    elif submitted in {"submitted", "transcoding", "reviewing"}:
        status, label, detail = "submitted", "已提交待审核", "已提交到B站，等待平台处理或审核。"
    elif submitted in {"queued", "uploading", "submitting"}:
        status, label, detail = "publishing", "投稿中", "正在排队、上传或提交，尚未确认公开。"
    elif submitted == "uncertain":
        status, label, detail = "publication_uncertain", "投稿结果待确认", "提交结果尚未确认，需要核对平台记录，不能直接重复投稿。"
    elif submitted == "failed":
        status, label, detail = "publication_failed", "投稿失败", "本次上传或提交失败，尚未确认投稿成功。"
    elif submitted == "private":
        status, label, detail = "private", "仅自己可见", "稿件在B站设为仅自己可见。"
    elif submitted == "rejected":
        status, label, detail = "rejected", "平台退回", "B站未通过这次稿件，需要核对平台审核结果。"
    elif review["status"] in {"held", "failed"}:
        status, label = "held", "审稿拦截"
        detail = REVIEW_REASONS.get(review.get("reason_code"), "自动审稿未通过，需要进一步核对。")
    elif review["status"] == "reviewing":
        status, label, detail = "reviewing", "自动审稿中", "正在检查完整语音、内容证据与投稿文案。"
    else:
        minimum = max(60.0, _number(policy.get("minimum_duration_seconds"), 60.0))
        created, eligible_after = _number(job.get("created_at")), _number(policy.get("eligible_after"), math.inf)
        if _number(job.get("duration")) < minimum:
            status, label = "below_minimum_duration", "时长不足"
            detail = f"片长不足{minimum:g}秒，不满足当前自动投稿时长要求。"
        elif batch_paused:
            status, label = "publication_paused", "发布批次暂停"
            detail = "素材在审稿后有变更，需重新核对并更新发布计划。"
        elif review["status"] == "approved":
            status, label = "ready", "审稿通过待投稿"
            detail = ("旧版自动审稿已通过；这是尚未提交的历史库存，不在当前实时投稿队列中。"
                      if review["source"] in {"legacy", "legacy_auto"} else "自动审稿已通过，尚未确认提交到B站。")
        elif (policy.get("enabled") is True and session.get("auto_publish") is True
              and created >= eligible_after):
            status, label, detail = "pending_review", "待自动审稿", "等待自动审稿，合格后进入投稿流程。"
        else:
            status, label = "not_reviewed", "未自动审稿"
            detail = ("历史片段未进入当前自动审稿流程，尚未提交。" if math.isfinite(eligible_after) and created < eligible_after
                      else "未记录自动审稿结果，尚未提交。")
    clip.update(status=status, status_label=label, status_detail=detail,
                review_required=status in {"held", "not_reviewed", "publication_uncertain",
                                           "publication_failed", "publication_paused", "rejected"})


class LibraryRefreshError(RuntimeError):
    """A fixed safe error code, without response data or private paths."""


def build_local_manifest(snapshot: dict, patrol: dict, data_dir: Path) -> dict:
    """Return completed live/imported clips and relative, prefix-safe media URLs."""
    data_dir = Path(data_dir).resolve()
    result = build_manifest(snapshot, patrol)
    jobs = {job.get("id"): job for job in snapshot.get("jobs", []) if isinstance(job, dict)}
    sessions = {session.get("id"): session for session in snapshot.get("sessions", []) if isinstance(session, dict)}
    publications = _publication_records(snapshot)
    paused_jobs = _paused_batch_jobs(data_dir)
    policy = snapshot.get("live_publication_policy")
    policy = policy if isinstance(policy, dict) else {}
    available = []
    for clip in result["clips"]:
        files = {}
        for name in LOCAL_FILES:
            try:
                root = data_dir / "exports"
                # safe_file checks the job and file; also reject a linked root.
                if root.is_symlink():
                    raise ManifestError("unsafe_export_path")
                path = safe_file(root, clip["id"], name)
                size = path.stat().st_size
            except (ManifestError, OSError):
                continue
            if size:
                files[name] = {"url": f"../../api/exports/{clip['id']}/{name}", "size": size}
        # Original ZIPs carry internal metadata. A job without its real video
        # is not a playable clip. Neither appears in the gallery.
        if "clip.mp4" in files:
            clip["files"] = files
            job = jobs.get(clip["id"], {})
            _display_status(clip, job, sessions.get(job.get("session_id"), {}), policy,
                            publications.get(clip["id"]), clip["id"] in paused_jobs)
            review = job.get("subtitle_review")
            if isinstance(review, dict):
                clip["subtitle_review"] = {
                    "applied": review.get("applied") is True,
                    "aligned": review.get("status") == "complete" and review.get("timing_basis") == "forced_alignment",
                }
            available.append(clip)
    result["clips"] = available
    result["counts"] = counts(available)
    result["counts"].update(
        published=sum(clip["status"] == "published" for clip in available),
        submitted=sum(clip["status"] in {"published", "submitted", "private", "rejected"} for clip in available),
        ready=sum(clip["status"] == "ready" for clip in available),
        held=sum(clip["status"] == "held" for clip in available),
    )
    return result


def _atomic_manifest(output_path: Path, manifest: dict) -> None:
    temporary = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         prefix=".library-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(manifest, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def refresh_library(
    data_dir: Path = ROOT / "data",
    output_path: Path = ROOT / "static" / "clips" / "library.json",
    base_url: str = "http://127.0.0.1:6880",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Atomically refresh display metadata, preserving it on upstream failure.

    Returns the new manifest; failures raise LibraryRefreshError containing
    only a fixed code suitable for the refresh loop's status/log output.
    """
    data_dir, output_path = Path(data_dir), Path(output_path)
    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10, follow_redirects=False,
            trust_env=False, transport=transport,
        ) as client:
            response = await client.get("/api/state")
            response.raise_for_status()
            snapshot = response.json()
        if not isinstance(snapshot, dict) or any(
            not isinstance(snapshot.get(key), list) for key in ("sessions", "jobs")
        ):
            raise ValueError("invalid snapshot")
        patrol = read_json(data_dir / "following-patrol" / "status.json", {})
        manifest = build_local_manifest(snapshot, patrol if isinstance(patrol, dict) else {}, data_dir)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
        raise LibraryRefreshError("workbench_unavailable") from None
    try:
        _atomic_manifest(output_path, manifest)
    except (OSError, ValueError, TypeError):
        raise LibraryRefreshError("library_write_failed") from None
    return manifest
