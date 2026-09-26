"""SQLite session ledger (FR-B6). Local only (§18). Never raises into the pipeline."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import aiosqlite

from typing import Any, Optional

from app.models import Claim, ClaimVerdict, CorrectionRecord, MessageNew, utc_now_iso

log = logging.getLogger("reigns.ledger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT, message_id TEXT, role TEXT, position INTEGER, text TEXT, ts TEXT,
  heat INTEGER,
  PRIMARY KEY (session_id, message_id));
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, quote TEXT, normalized TEXT,
  type TEXT, risk TEXT, source_ref TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, claim_id TEXT, detector TEXT,
  status TEXT, confidence REAL, evidence TEXT, explanation TEXT, latency_ms INTEGER,
  final TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY, session_id TEXT, prompt_type TEXT, level INTEGER,
  text TEXT, variant_id TEXT, inserted INTEGER DEFAULT 0, fixed INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, claim_id TEXT, kind TEXT,
  note TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS chats (
  chat_key TEXT PRIMARY KEY, session_id TEXT, heat INTEGER, level INTEGER,
  red_count INTEGER, amber_count INTEGER, bubble TEXT, updated_at TEXT);
"""
# Columns added after a table first shipped: (table, column, type). Older reigns.db files get
# them via ALTER TABLE on open, so nobody has to delete their database.
MIGRATIONS = [("messages", "heat", "INTEGER")]


class Ledger:
    def __init__(self, path: str | None = None) -> None:
        default = Path(__file__).resolve().parents[1] / os.environ.get("REIGNS_DB", "reigns.db")
        self.path = path or str(default)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        try:
            self._db = await aiosqlite.connect(self.path)
            await self._db.executescript(SCHEMA)
            for table, column, type_ in MIGRATIONS:
                async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                    have = {row[1] for row in await cur.fetchall()}
                if column not in have:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            await self._db.commit()
        except Exception as exc:
            log.error("ledger open failed (continuing without ledger): %s", exc)
            self._db = None

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _run(self, sql: str, rows: list[tuple]) -> None:
        if not self._db or not rows:
            return
        try:
            await self._db.executemany(sql, rows)
            await self._db.commit()
        except Exception as exc:
            log.error("ledger write failed: %s", exc)

    async def message(self, session_id: str, msg: MessageNew, heat: Optional[int] = None) -> None:
        await self._run(
            "INSERT OR REPLACE INTO messages (session_id, message_id, role, position, text, ts, heat)"
            " VALUES (?,?,?,?,?,?,?)",
            [(session_id, msg.message_id, msg.role, msg.position, msg.text, utc_now_iso(), heat)],
        )

    async def message_heat(self, session_id: str, message_id: str, heat: int) -> None:
        """Heat (panic rating) right after this reply was judged."""
        await self._run(
            "UPDATE messages SET heat = ? WHERE session_id = ? AND message_id = ?",
            [(heat, session_id, message_id)],
        )

    # ---------------------------------------------------------------- per-chat heat
    async def save_chat_heat(
        self, chat_key: str, session_id: str, heat: int, level: int, red: int, amber: int
    ) -> None:
        await self._run(
            "INSERT INTO chats (chat_key, session_id, heat, level, red_count, amber_count, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(chat_key) DO UPDATE SET session_id=excluded.session_id,"
            " heat=excluded.heat, level=excluded.level, red_count=excluded.red_count,"
            " amber_count=excluded.amber_count, updated_at=excluded.updated_at",
            [(chat_key, session_id, heat, level, red, amber, utc_now_iso())],
        )

    async def save_chat_bubble(self, chat_key: str, bubble_json: str) -> None:
        await self._run(
            "INSERT INTO chats (chat_key, bubble, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(chat_key) DO UPDATE SET bubble=excluded.bubble,"
            " updated_at=excluded.updated_at",
            [(chat_key, bubble_json, utc_now_iso())],
        )

    async def load_chat(self, chat_key: str) -> Optional[dict[str, Any]]:
        """Saved heat/level/counts/bubble for a chat, or None. Never raises."""
        if not self._db:
            return None
        try:
            async with self._db.execute(
                "SELECT heat, level, red_count, amber_count, bubble FROM chats WHERE chat_key = ?",
                (chat_key,),
            ) as cur:
                row = await cur.fetchone()
        except Exception as exc:
            log.error("ledger read failed: %s", exc)
            return None
        if row is None:
            return None
        heat, level, red, amber, bubble = row
        return {"heat": heat or 0, "level": level or 0, "red_count": red or 0,
                "amber_count": amber or 0, "bubble": bubble}

    async def claims(self, session_id: str, claims: list[Claim]) -> None:
        await self._run(
            "INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (c.claim_id, session_id, c.message_id, c.quote, c.normalized, c.type, c.risk,
                 c.source_ref, utc_now_iso())
                for c in claims
            ],
        )

    async def verdicts(self, session_id: str, verdicts: list[ClaimVerdict]) -> None:
        rows = []
        for v in verdicts:
            for r in v.detector_results:  # every verdict stores evidence + detector name
                rows.append(
                    (None, session_id, v.claim_id, r.detector, r.status, r.confidence,
                     json.dumps([e.model_dump() for e in r.evidence]), r.explanation,
                     r.latency_ms, v.final, utc_now_iso())
                )
        await self._run("INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    async def correction(self, session_id: str, c: CorrectionRecord) -> None:
        await self._run(
            "INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?,?,?)",
            [(c.correction_id, session_id, c.prompt_type, c.level, c.text, c.variant_id,
              int(c.inserted), None if c.fixed is None else int(c.fixed), utc_now_iso())],
        )

    async def feedback(self, session_id: str, claim_id: str, kind: str, note: str | None) -> None:
        await self._run(
            "INSERT INTO feedback VALUES (?,?,?,?,?,?)",
            [(None, session_id, claim_id, kind, note, utc_now_iso())],
        )
