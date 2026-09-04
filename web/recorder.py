"""مسجل أحداث SQLite للوكيل واللوحة — مكتبة معيارية خفيفة بلا اعتماديات خارجية.

SQLite event recorder shared by the agent and the dashboard.

The agent appends run/event rows while it works; the dashboard reads them for
the UI and pushes live updates over WebSocket.  Standard-library only
(``sqlite3``) so ``agent.py`` can import it in any environment, including the
scheduled GitHub Actions cron.
"""

import datetime as _dt
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: المسار الافتراضي لقاعدة البيانات (يُضبط من CELIA_DB_PATH).
DEFAULT_DB_PATH = os.environ.get("CELIA_DB_PATH") or os.path.join("data", "celia.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    mode TEXT NOT NULL DEFAULT '',
    repos_scanned INTEGER NOT NULL DEFAULT 0,
    prs_created INTEGER NOT NULL DEFAULT 0,
    comments_posted INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    repo TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""

_local = threading.local()


def now_iso() -> str:
    """طابع زمني ISO-8601 متفق عليه (UTC)."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _utc_parse(value: str) -> str:
    """تطبيع أي طابع ISO إلى ثوانٍ (آمن للعرض)."""
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, AttributeError, TypeError):
        return value or now_iso()


class Recorder:
    """كتابة وقراءة سجلات تشغيل الوكيل في قاعدة SQLite واحدة.

    Thread-safe عبر sqlite3 connections لكل خيط + WAL لدعم كتّاب متعددين
    (الوكيل في عملية واللوحة في عملية أخرى).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

    # ------------------------------------------------------------------ #
    # Connection handling
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(_local, "conn") or _local.conn is None:
            _local.conn = self._connect()
        return _local.conn

    def close(self) -> None:
        if hasattr(_local, "conn") and _local.conn is not None:
            _local.conn.close()
            _local.conn = None

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def start_run(self, mode: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
        """تسجيل بداية تشغيل للوكيل وإرجاع رقمه (run id)."""
        conn = self._conn
        cur = conn.execute(
            "INSERT INTO runs (started_at, status, mode, meta) VALUES (?, 'running', ?, ?)",
            (now_iso(), mode or "", json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str = "finished",
        repos_scanned: int = 0,
        prs_created: int = 0,
        comments_posted: int = 0,
        errors: int = 0,
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at=?, status=?, repos_scanned=?, "
            "prs_created=?, comments_posted=?, errors=? WHERE id=?",
            (now_iso(), status, repos_scanned, prs_created, comments_posted, errors, run_id),
        )
        self._conn.commit()

    def add_event(
        self,
        run_id: Optional[int],
        level: str = "info",
        message: str = "",
        repo: Optional[str] = None,
    ) -> int:
        """إضافة حدث واحد (يُكتب ويُبث). آمنة للفشل: لا تُسقط التشغيل الرئيسي."""
        try:
            conn = self._conn
            cur = conn.execute(
                "INSERT INTO events (run_id, ts, level, repo, message) VALUES (?, ?, ?, ?, ?)",
                (run_id, now_iso(), (level or "info").lower(), repo, message or ""),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception as exc:  # noqa: BLE001 - التسجيل لا يجب أن يكسر الوكيل
            logger.warning("تعذّر تسجيل حدث في SQLite: %s", exc)
            return -1

    # ------------------------------------------------------------------ #
    # Reads (dashboard)
    # ------------------------------------------------------------------ #
    def get_runs(self, limit: int = 25) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()
        return dict(row) if row else None

    def get_events(self, run_id: Optional[int] = None, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        if run_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (int(run_id), int(after_id), int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?",
                (int(after_id), int(limit)),
            ).fetchall()
            rows = list(reversed(rows))
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total_runs, "
            "COALESCE(SUM(prs_created),0) AS total_prs, "
            "COALESCE(SUM(comments_posted),0) AS total_comments, "
            "COALESCE(SUM(errors),0) AS total_errors, "
            "COALESCE(MAX(id),0) AS last_run_id FROM runs"
        ).fetchone()
        return dict(row)

    def delete_all(self) -> None:
        self._conn.execute("DELETE FROM events")
        self._conn.execute("DELETE FROM runs")
        self._conn.commit()


def demo_seed(recorder: Recorder, runs: int = 3, events_per_run: int = 6) -> None:
    """بذر بيانات تجريبية واقعية (للعارض/المعاينة فقط) عندما تكون القاعدة فارغة."""
    if recorder.get_runs(limit=1):
        return  # ليست فارغة
    samples = [
        ("🚀 بدء تشغيل وكيل فحص المستودعات (mode=auto)", "info"),
        ("🔍 فحص المستودع: elazamey/cela.1", "info"),
        ("🛡️ رصد ثغرة npm في lodash داخل package.json", "warning"),
        ("🎉 تم إنشاء Pull Request بنجاح: #3", "success"),
        ("🩺 تشخيص فشل CI في .github/workflows/ci.yml", "info"),
        ("🏁 انتهت عملية الفحص والإصلاح.", "success"),
    ]
    for i in range(runs, 0, -1):
        run_id = recorder.start_run(mode="demo")
        import time as _t

        for j in range(events_per_run):
            text, level = samples[(i + j) % len(samples)]
            recorder.add_event(run_id, level=level, message=text)
            _t.sleep(0.05)
        recorder.finish_run(
            run_id,
            repos_scanned=3 + i,
            prs_created=1 if i > 1 else 0,
            comments_posted=i - 1,
            errors=i % 2,
        )
