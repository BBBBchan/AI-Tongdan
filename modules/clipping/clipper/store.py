"""SQLite journal: session clocks, immutable source events and editable highlights."""
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def uid():
    return uuid.uuid4().hex[:16]


def serialized(method):
    """Keep a connection's whole operation atomic across calling threads."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class Store:
    """One shared connection; all synchronous public operations are serialized.

    SQLite serializes database writers, but it does not isolate transactions or
    read/modify/write sequences between threads using the same connection.
    RLock permits update methods to call read methods within one critical section.
    """
    def __init__(self, directory):
        self._lock = threading.RLock()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "clipper.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, created REAL, updated REAL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, start REAL, end REAL,
          text TEXT, data TEXT, source_key TEXT, UNIQUE(session_id,source_key));
        CREATE INDEX IF NOT EXISTS event_time ON events(session_id,start);
        CREATE TABLE IF NOT EXISTS segments(
          id TEXT PRIMARY KEY, session_id TEXT, start REAL, end REAL, path TEXT,
          metadata TEXT, UNIQUE(session_id,path));
        CREATE TABLE IF NOT EXISTS anchors(
          id TEXT PRIMARY KEY, session_id TEXT, created REAL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, session_id TEXT, anchor_id TEXT, created REAL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publications(
          id TEXT PRIMARY KEY, job_id TEXT UNIQUE, created REAL, payload TEXT NOT NULL);
        """)
        self.db.commit()
        # Interrupted processes cannot be reported as still running after restart.
        for s in self.sessions():
            if s.get("analysis_source_reserved"):
                self.update_session(s["id"], analysis_source_reserved=False,
                                    analysis_status="分析已中断")
            if s.get("status") in {"live", "connecting", "analyzing", "reconnecting", "waiting", "recording"}:
                self.update_session(s["id"], status="interrupted", warning="进程重启，采集已停止；已有素材和锚点已保留")
            if s.get("analysis_status") in {"running", "transcribing", "pending", "analyzing",
                                           "分析中", "正在提取画面与语音"}:
                self.update_session(s["id"], analysis_status="分析已中断",
                                    analysis_warning="进程重启，请重新分析；已完成的语音转写会被复用")
        for j in self.jobs():
            if j.get("status") in {"queued", "rendering"}:
                self.update_job(j["id"], status="failed", error="导出被进程重启中断，请重试")
        for p in self.publications():
            if p["status"] == "submitting":
                self.update_publication(p["id"], status="uncertain",
                                        error="提交时进程中断，结果待核实；请先检查B站创作中心，勿重复投稿")
            elif p["status"] in {"queued", "uploading"}:
                self.update_publication(p["id"], status="failed",
                                        error="上传被进程重启中断，可重试；尚未提交稿件")

    @serialized
    def sessions(self):
        return [json.loads(x["payload"]) for x in self.db.execute("SELECT payload FROM sessions ORDER BY created DESC")]

    @serialized
    def session(self, sid):
        x = self.db.execute("SELECT payload FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(x["payload"]) if x else None

    @serialized
    def create_session(self, **values):
        s = {"id": uid(), "created_at": time.time(), "duration": 0, "status": "ready",
             "auto_render": True, "auto_publish": False, "minimum_score": 88, "style": "reaction", **values}
        self.db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                        (s["id"], s["created_at"], time.time(), json.dumps(s, ensure_ascii=False)))
        self.db.commit()
        return s

    @serialized
    def update_session(self, sid, **values):
        s = self.session(sid)
        if s is None:
            raise KeyError(sid)
        s.update(values)
        self.db.execute("UPDATE sessions SET updated=?,payload=? WHERE id=?",
                        (time.time(), json.dumps(s, ensure_ascii=False), sid))
        self.db.commit()
        return s

    @serialized
    def add_event(self, sid, kind, start, end, text, data=None, source_key=None):
        key = uid()
        self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)",
                        (key, sid, kind, float(start), float(end), str(text)[:5000],
                         json.dumps(data or {}, ensure_ascii=False), source_key))
        self.db.commit()
        return key

    @serialized
    def events(self, sid, start=0, end=1e12, limit=2000):
        # Time overlap preserves utterances straddling an editorial boundary.
        rows = self.db.execute("SELECT * FROM events WHERE session_id=? AND end>=? AND start<=? ORDER BY start LIMIT ?",
                               (sid, start, end, limit))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    @serialized
    def add_segment(self, sid, start, end, path, metadata=None):
        self.db.execute("INSERT OR REPLACE INTO segments VALUES(?,?,?,?,?,?)",
                        (uid(), sid, start, end, str(path), json.dumps(metadata or {})))
        self.db.commit()

    @serialized
    def segments(self, sid, start=0, end=1e12):
        rows = [{**dict(r), "metadata": json.loads(r["metadata"])} for r in self.db.execute(
            "SELECT * FROM segments WHERE session_id=? AND end>? AND start<? ORDER BY start", (sid, start, end))]
        return [row for row in rows if not row["metadata"].get("retention_removed")]

    @serialized
    def reserve_analysis(self, sid, **values):
        """Make queued analysis visible to the independent source-buffer reaper."""
        from .recording_retention import source_available
        try:
            self.db.execute("BEGIN IMMEDIATE")
            session = self.session(sid)
            if session is None:
                raise KeyError(sid)
            if not source_available(session):
                raise ValueError("原录播缓冲已清理；已完成切片仍可预览和下载")
            session.update(analysis_status="pending", analysis_source_reserved=True, **values)
            self.db.execute("UPDATE sessions SET updated=?,payload=? WHERE id=?",
                            (time.time(), json.dumps(session, ensure_ascii=False), sid))
            self.db.commit()
            return session
        except BaseException:
            self.db.rollback()
            raise

    @serialized
    def anchors(self, sid=None):
        rows = self.db.execute("SELECT payload FROM anchors" + (" WHERE session_id=?" if sid else "") +
                               " ORDER BY created DESC", (sid,) if sid else ())
        return [json.loads(x["payload"]) for x in rows]

    @serialized
    def anchor(self, aid):
        x = self.db.execute("SELECT payload FROM anchors WHERE id=?", (aid,)).fetchone()
        return json.loads(x["payload"]) if x else None

    @serialized
    def add_anchor(self, sid, **values):
        a = {"id": uid(), "session_id": sid, "created_at": time.time(),
             "origin": "automatic", "status": "ready", "style": "reaction", **values}
        self.db.execute("INSERT INTO anchors VALUES(?,?,?,?)",
                        (a["id"], sid, a["created_at"], json.dumps(a, ensure_ascii=False)))
        self.db.commit()
        return a

    @serialized
    def update_anchor(self, aid, **values):
        a = self.anchor(aid)
        if a is None:
            raise KeyError(aid)
        a.update(values)
        self.db.execute("UPDATE anchors SET payload=? WHERE id=?", (json.dumps(a, ensure_ascii=False), aid))
        self.db.commit()
        return a

    @serialized
    def jobs(self, sid=None):
        rows = self.db.execute("SELECT payload FROM jobs" + (" WHERE session_id=?" if sid else "") +
                               " ORDER BY created DESC", (sid,) if sid else ())
        return [json.loads(x["payload"]) for x in rows]

    @serialized
    def job(self, jid):
        x = self.db.execute("SELECT payload FROM jobs WHERE id=?", (jid,)).fetchone()
        return json.loads(x["payload"]) if x else None

    @serialized
    def create_job(self, aid, sid, **values):
        from .recording_retention import source_available
        j = {"id": uid(), "anchor_id": aid, "session_id": sid, "created_at": time.time(),
             "status": "queued", "progress": 0, **values}
        try:
            self.db.execute("BEGIN IMMEDIATE")
            session = self.session(sid)
            if session is not None and not source_available(session):
                raise ValueError("原录播缓冲已清理；已完成切片仍可预览和下载")
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)",
                            (j["id"], sid, aid, j["created_at"], json.dumps(j, ensure_ascii=False)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return j

    @serialized
    def update_job(self, jid, **values):
        j = self.job(jid)
        if j is None:
            raise KeyError(jid)
        j.update(values)
        self.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(j, ensure_ascii=False), jid))
        self.db.commit()
        return j

    @serialized
    def publications(self):
        return [json.loads(x["payload"]) for x in self.db.execute(
            "SELECT payload FROM publications ORDER BY created DESC")]

    @serialized
    def publication(self, pid):
        row = self.db.execute("SELECT payload FROM publications WHERE id=?", (pid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    @serialized
    def publication_for_job(self, jid):
        row = self.db.execute("SELECT payload FROM publications WHERE job_id=?", (jid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    @serialized
    def create_publication(self, jid, **values):
        p = {"id": uid(), "job_id": jid, "created_at": time.time(),
             "status": "queued", "progress": 0, **values}
        self.db.execute("INSERT INTO publications VALUES(?,?,?,?)",
                        (p["id"], jid, p["created_at"], json.dumps(p, ensure_ascii=False)))
        self.db.commit()
        return p

    @serialized
    def update_publication(self, pid, **values):
        p = self.publication(pid)
        if p is None:
            raise KeyError(pid)
        p.update(values)
        self.db.execute("UPDATE publications SET payload=? WHERE id=?",
                        (json.dumps(p, ensure_ascii=False), pid))
        self.db.commit()
        return p

    @serialized
    def close(self):
        self.db.close()
