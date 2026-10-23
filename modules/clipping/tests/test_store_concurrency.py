from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from clipper.store import Store


@pytest.mark.parametrize("entity", ["session", "anchor", "job", "publication"])
def test_concurrent_payload_updates_preserve_both_fields(tmp_path, monkeypatch, entity):
    store = Store(tmp_path)
    session = store.create_session()
    anchor = store.add_anchor(session["id"])
    job = store.create_job(anchor["id"], session["id"])
    publication = store.create_publication(job["id"])
    row = {"session": session, "anchor": anchor, "job": job, "publication": publication}[entity]
    getter = getattr(store, entity)
    update = getattr(store, "update_" + entity)
    first_read, release, second_started, second_read = [threading.Event() for _ in range(4)]
    first_thread = []

    def controlled_read(key):
        result = getter(key)
        if threading.get_ident() == first_thread[0]:
            first_read.set()
            assert release.wait(3), "first updater was never released"
        else:
            second_read.set()
        return result

    monkeypatch.setattr(store, entity, controlled_read)

    def first():
        first_thread.append(threading.get_ident())
        return update(row["id"], first_value="retained")

    def second():
        second_started.set()
        return update(row["id"], second_value="retained")

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            left = workers.submit(first)
            right = None
            try:
                assert first_read.wait(3)
                right = workers.submit(second)
                assert second_started.wait(3)
                # Hold the first operation between its SELECT and UPDATE. The
                # other updater must not obtain a stale JSON payload here.
                assert not second_read.wait(.1)
            finally:
                release.set()
            left.result(timeout=3)
            assert right is not None
            right.result(timeout=3)
        final = getter(row["id"])
        assert final["first_value"] == final["second_value"] == "retained"
    finally:
        store.close()


@pytest.mark.parametrize("operation, removed", [
    ("reserve_analysis", False), ("create_job", False),
    ("reserve_analysis", True), ("create_job", True),
])
def test_other_writes_cannot_enter_explicit_transaction_before_commit_or_rollback(
        tmp_path, operation, removed):
    store = Store(tmp_path)
    session = store.create_session(source_retention={"status": "removed"} if removed else {})
    connection = store.db
    begun, release, second_started, other_sql = [threading.Event() for _ in range(4)]
    first_thread = []

    class PausedTransaction:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, *args):
            if threading.get_ident() != first_thread[0]:
                other_sql.set()
            result = connection.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                begun.set()
                assert release.wait(3), "transaction was never released"
            return result

    store.db = PausedTransaction()

    def first():
        first_thread.append(threading.get_ident())
        if operation == "reserve_analysis":
            return store.reserve_analysis(session["id"])
        return store.create_job("fixture-anchor", session["id"])

    def second():
        second_started.set()
        return store.add_event(session["id"], "transcript", 0, 1, "must survive rollback")

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            left = workers.submit(first)
            right = None
            try:
                assert begun.wait(3)
                right = workers.submit(second)
                assert second_started.wait(3)
                # An unrelated commit must neither finish this transaction nor
                # accidentally join the transaction that is about to roll back.
                assert not other_sql.wait(.1)
            finally:
                release.set()
            if removed:
                with pytest.raises(ValueError, match="原录播缓冲已清理"):
                    left.result(timeout=3)
            else:
                left.result(timeout=3)
            assert right is not None
            right.result(timeout=3)
        assert [event["text"] for event in store.events(session["id"])] == ["must survive rollback"]
        assert not connection.in_transaction
    finally:
        store.close()
