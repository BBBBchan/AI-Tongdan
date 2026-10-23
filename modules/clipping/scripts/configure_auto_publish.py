#!/usr/bin/env python3
"""Explicitly enable or disable reviewed live publishing for your own account."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipper.config import Settings
from clipper.live_publishing import atomic_json, publication_policy, POLICY_NAME
from clipper.publishing import PublicationManager


async def configure(settings, command, account_uid=None, *, manager=None):
    policy = publication_policy(settings)
    if command == "enable":
        if type(account_uid) is not int or account_uid <= 0:
            raise ValueError("启用时必须指定自己的正整数 --account-uid")
        manager = manager or PublicationManager(settings, store=None)
        try:
            account = await manager.account(fresh=True)
        except Exception:
            raise ValueError("无法验证投稿账号，请检查登录配置后重试") from None
        if not account.get("logged_in") or str(account.get("uid")) != str(account_uid):
            raise ValueError("投稿 Cookie 未登录或账号与 --account-uid 不一致")
        continuing = policy.get("enabled") is True and policy.get("account_uid") == account_uid
        if policy.get("enabled") and not continuing:
            raise ValueError("更换账号前请先 disable，再为新账号 enable")
        now = time.time()
        policy.update(version=1, enabled=True, account_uid=account_uid,
                      eligible_after=policy.get("eligible_after", now) if continuing else now,
                      activated_at=now, minimum_duration_seconds=60, allow_source_overlap=False,
                      submission_mode="immediate_after_quality_review")
    elif command == "disable":
        policy.update(enabled=False, disabled_at=time.time())
        # Remove a legacy deferred activation instruction, if present.
        policy.pop("activate_after_batch", None)
        policy.pop("activate_after_plan", None)
    elif command != "status":
        raise ValueError("未知命令")
    if command != "status":
        atomic_json(settings.data_dir / POLICY_NAME, policy)
    return {key: policy.get(key) for key in
            ("enabled", "account_uid", "eligible_after", "minimum_duration_seconds", "submission_mode")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "enable", "disable"])
    parser.add_argument("--account-uid", type=int)
    args = parser.parse_args()
    try:
        result = asyncio.run(configure(Settings.from_env(), args.command, args.account_uid))
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
