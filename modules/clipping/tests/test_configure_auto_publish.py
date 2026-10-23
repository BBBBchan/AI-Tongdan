import asyncio
from types import SimpleNamespace

import pytest

from clipper.config import Settings
from clipper.live_publishing import publication_policy, atomic_json, POLICY_NAME
from scripts.configure_auto_publish import configure


class Account:
    def __init__(self, uid=42, logged_in=True):
        self.uid, self.logged_in = uid, logged_in
    async def account(self, fresh=False):
        assert fresh
        return {"logged_in": self.logged_in, "uid": self.uid}


@pytest.mark.parametrize("account", [Account(99), Account(42, False)])
def test_wrong_or_expired_account_cannot_enable(tmp_path, account):
    settings = Settings(data_dir=tmp_path)
    with pytest.raises(ValueError, match="账号"):
        asyncio.run(configure(settings, "enable", 42, manager=account))
    assert not (tmp_path / POLICY_NAME).exists()


def test_enable_only_admits_future_clips_and_is_idempotent(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = asyncio.run(configure(settings, "enable", 42, manager=Account()))
    second = asyncio.run(configure(settings, "enable", 42, manager=Account()))
    assert first["enabled"] and first["eligible_after"] > 0
    assert first["eligible_after"] == second["eligible_after"]
    assert publication_policy(settings)["allow_source_overlap"] is False
    assert (tmp_path / POLICY_NAME).stat().st_mode & 0o777 == 0o600


def test_disable_cannot_be_reactivated_by_legacy_batch(tmp_path):
    settings = Settings(data_dir=tmp_path)
    atomic_json(tmp_path / POLICY_NAME, {"enabled": True, "activate_after_batch": "old", "activate_after_plan": "old"})
    asyncio.run(configure(settings, "disable"))
    policy = publication_policy(settings)
    assert not policy["enabled"] and "activate_after_batch" not in policy


def test_status_does_not_create_files(tmp_path):
    asyncio.run(configure(Settings(data_dir=tmp_path), "status"))
    assert list(tmp_path.iterdir()) == []
