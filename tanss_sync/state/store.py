"""SQLite-Zustandsspeicher.

Die einzige veränderliche Datei im Betrieb. Läuft im WAL-Modus, weil Listener-Thread
und Sync-Worker in dieselbe Datei schreiben — ohne WAL blockieren sie sich gegenseitig
und laufen in ``database is locked``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..domain.identity import SyncKey
from .records import EmergencyStop, LinkRecord, RunReport

SCHEMA = Path(__file__).with_name("schema.sql")


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- Verbindung

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def migrate(self) -> None:
        """Schema anlegen bzw. fortschreiben. Idempotent."""
        self.connect().executescript(SCHEMA.read_text(encoding="utf-8"))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StateStore:
        self.migrate()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- Verknüpfungen

    def get_link(self, key: SyncKey) -> LinkRecord | None:
        row = self.connect().execute(
            "SELECT * FROM links WHERE mailbox=? AND uid=? AND sequence=? AND travel_role=?",
            (key.mailbox, key.uid, key.sequence, key.travel_role),
        ).fetchone()
        return LinkRecord.from_row(row) if row else None

    def get_link_by_support(self, support_id: int) -> LinkRecord | None:
        if support_id <= 0:  # Occurrences haben 0 - darueber ist nichts auffindbar
            return None
        row = self.connect().execute(
            "SELECT * FROM links WHERE tanss_support_id=? AND travel_role='main'",
            (support_id,),
        ).fetchone()
        return LinkRecord.from_row(row) if row else None

    def get_link_by_event(self, mailbox: str, event_id: str) -> LinkRecord | None:
        row = self.connect().execute(
            "SELECT * FROM links WHERE mailbox=? AND graph_event_id=?",
            (mailbox, event_id),
        ).fetchone()
        return LinkRecord.from_row(row) if row else None

    def upsert_link(self, link: LinkRecord) -> None:
        """Verknüpfung anlegen oder fortschreiben.

        Prüft ``write_direction`` als Zusicherung: Eine Fahrt- oder Abwesenheitszeile
        mit ``both`` wäre ein stillschweigend abgeschalteter Schreibschutz. Das bricht
        ab, statt sich selbst zu korrigieren — eine stille Korrektur würde den Fehler
        verbergen, statt ihn zu melden.
        """
        link.assert_consistent()
        data = link.to_row()
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{c}=excluded.{c}" for c in data
                            if c not in ("mailbox", "uid", "sequence", "travel_role"))
        self.connect().execute(
            f"INSERT INTO links ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(mailbox, uid, sequence, travel_role) DO UPDATE SET {updates}",
            tuple(data.values()),
        )

    def set_link_state(self, key: SyncKey, state: str) -> None:
        self.connect().execute(
            "UPDATE links SET state=?, last_seen_at=? "
            "WHERE mailbox=? AND uid=? AND sequence=? AND travel_role=?",
            (state, _now(), key.mailbox, key.uid, key.sequence, key.travel_role),
        )

    def links_for_user(self, employee_id: int,
                       *, only_linked: bool = True) -> list[LinkRecord]:
        sql = "SELECT * FROM links WHERE tanss_employee_id=?"
        if only_linked:
            sql += " AND state='linked'"
        rows = self.connect().execute(sql, (employee_id,)).fetchall()
        return [LinkRecord.from_row(r) for r in rows]

    def count_links(self, employee_id: int) -> int:
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM links WHERE tanss_employee_id=? AND state='linked'",
            (employee_id,),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------- vorgemerkte Löschungen

    def pending_deletion(self, key: SyncKey, side: str) -> sqlite3.Row | None:
        """Eine offene Vormerkung zu diesem Termin — noch nicht ausgeführt, nicht zurückgenommen."""
        return self.connect().execute(
            "SELECT * FROM pending_deletions WHERE mailbox=? AND uid=? AND sequence=? "
            "AND travel_role=? AND side=? AND executed_at IS NULL "
            "AND cancelled_at IS NULL ORDER BY id DESC LIMIT 1",
            (key.mailbox, key.uid, key.sequence, key.travel_role, side),
        ).fetchone()

    def mark_deletion_executed(self, row_id: int) -> None:
        self.connect().execute(
            "UPDATE pending_deletions SET executed_at=? WHERE id=?", (_now(), row_id))

    def cancel_deletion(self, key: SyncKey, side: str, reason: str) -> int:
        """Nimmt offene Vormerkungen zurück — der Termin ist wieder aufgetaucht.

        Das ist der Sinn der Karenzzeit: Beim Wandeln einer Vormerkung in einen festen
        Termin verschwindet der Datensatz für Sekunden und kommt dann zurück.
        """
        cur = self.connect().execute(
            "UPDATE pending_deletions SET cancelled_at=?, cancel_reason=? "
            "WHERE mailbox=? AND uid=? AND sequence=? AND travel_role=? AND side=? "
            "AND executed_at IS NULL AND cancelled_at IS NULL",
            (_now(), reason, key.mailbox, key.uid, key.sequence, key.travel_role, side),
        )
        return cur.rowcount

    # ---------------------------------------------------------------- Läufe

    def begin_run(self) -> int:
        cur = self.connect().execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, report: RunReport) -> None:
        self.connect().execute(
            "UPDATE runs SET finished_at=?, created=?, updated=?, deleted=?, "
            "skipped=?, errors=?, aborted_reason=?, detail=? WHERE id=?",
            (_now(), report.created, report.updated, report.deleted, report.skipped,
             report.errors, report.aborted_reason,
             json.dumps(report.detail, ensure_ascii=False), run_id),
        )

    def last_run(self) -> dict | None:
        row = self.connect().execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def consecutive_failed_runs(self) -> int:
        """Wie viele Läufe in Folge zuletzt gescheitert sind — für die Alarmschwelle."""
        rows = self.connect().execute(
            "SELECT errors, aborted_reason FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 50").fetchall()
        count = 0
        for row in rows:
            if row["errors"] or row["aborted_reason"]:
                count += 1
            else:
                break
        return count

    # ---------------------------------------------------------------- Not-Aus

    def raise_emergency(self, *, scope: str, kind: str, counted: int, threshold: int,
                        reason: str, run_id: int | None = None) -> int:
        existing = self.active_emergency(scope)
        if existing:
            return existing.id
        cur = self.connect().execute(
            "INSERT INTO emergency_stop (triggered_at, run_id, scope, kind, counted, "
            "threshold, reason) VALUES (?,?,?,?,?,?,?)",
            (_now(), run_id, scope, kind, counted, threshold, reason),
        )
        return int(cur.lastrowid)

    def active_emergency(self, scope: str | None = None) -> EmergencyStop | None:
        sql = "SELECT * FROM emergency_stop WHERE released_at IS NULL"
        args: tuple = ()
        if scope:
            sql += " AND scope=?"
            args = (scope,)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.connect().execute(sql, args).fetchone()
        return EmergencyStop.from_row(row) if row else None

    def ack_emergency(self, stop_id: int) -> None:
        """Quittiert den Alarm. **Hebt die Sperre nicht auf.**"""
        self.connect().execute(
            "UPDATE emergency_stop SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL",
            (_now(), stop_id),
        )

    def release_emergency(self, stop_id: int, by: str) -> None:
        """Hebt die Sperre auf — nur über ``--allow-bulk-delete``."""
        self.connect().execute(
            "UPDATE emergency_stop SET released_at=?, released_by=? WHERE id=?",
            (_now(), by, stop_id),
        )

    # ---------------------------------------------------------------- Delta

    def get_delta(self, mailbox: str) -> dict | None:
        row = self.connect().execute(
            "SELECT * FROM delta_tokens WHERE mailbox=?", (mailbox,)).fetchone()
        return dict(row) if row else None

    def set_delta(self, mailbox: str, deltalink: str,
                  window_start: datetime, window_end: datetime) -> None:
        self.connect().execute(
            "INSERT INTO delta_tokens (mailbox, deltalink, window_start, window_end, "
            "updated_at) VALUES (?,?,?,?,?) ON CONFLICT(mailbox) DO UPDATE SET "
            "deltalink=excluded.deltalink, window_start=excluded.window_start, "
            "window_end=excluded.window_end, updated_at=excluded.updated_at",
            (mailbox, deltalink, int(window_start.timestamp()),
             int(window_end.timestamp()), _now()),
        )

    def needs_rebase(self, mailbox: str, margin_days: int) -> bool:
        state = self.get_delta(mailbox)
        if not state or not state.get("window_end"):
            return True
        remaining = _ts(state["window_end"]) - datetime.now(UTC)
        return remaining.days < margin_days

    # ---------------------------------------------------------------- Sperren

    def acquire_lease(self, scope: str, holder: str, ttl_seconds: int) -> bool:
        now = _now()
        conn = self.connect()
        conn.execute("DELETE FROM leases WHERE expires_at < ?", (now,))
        try:
            conn.execute(
                "INSERT INTO leases (scope, holder, acquired_at, expires_at) VALUES (?,?,?,?)",
                (scope, holder, now, now + ttl_seconds),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def renew_lease(self, scope: str, holder: str, ttl_seconds: int) -> bool:
        cur = self.connect().execute(
            "UPDATE leases SET expires_at=? WHERE scope=? AND holder=?",
            (_now() + ttl_seconds, scope, holder),
        )
        return cur.rowcount > 0

    def release_lease(self, scope: str, holder: str) -> None:
        self.connect().execute("DELETE FROM leases WHERE scope=? AND holder=?",
                               (scope, holder))

    def holds_lease(self, scope: str, holder: str) -> bool:
        row = self.connect().execute(
            "SELECT 1 FROM leases WHERE scope=? AND holder=? AND expires_at >= ?",
            (scope, holder, _now()),
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- Wartung

    def vacuum(self) -> None:
        self.connect().execute("VACUUM")

    def integrity_check(self) -> bool:
        row = self.connect().execute("PRAGMA integrity_check").fetchone()
        return row[0] == "ok"

    def backup_to(self, target: str | Path) -> None:
        """Konsistente Sicherung über die SQLite-Backup-Schnittstelle."""
        target = Path(target).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as dest:
            self.connect().backup(dest)

    def stats(self) -> dict[str, int]:
        conn = self.connect()
        out = {}
        for table in ("links", "audit", "runs", "deleted_backup",
                      "pending_events", "pending_deletions"):
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out
