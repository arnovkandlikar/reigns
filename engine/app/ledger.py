"""SQLite session ledger (FR-B6). Local only (§18). Never raises into the pipeline."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import aiosqlite

from app.models import Claim, ClaimVerdict, CorrectionRecord, MessageNew, utc_now_iso

log = logging.getLogger("reigns.ledger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT, message_id TEXT, role TEXT, position INTEGER, text TEXT, ts TEXT,
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
"""


class Ledger:
    def __init__(self, path: str | None = None) -> None:
        default = Path(__file__).resolve().parents[1] / os.environ.get("REIGNS_DB", "reigns.db")
        self.path = path or str(default)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        try:
            self._db = await aiosqlite.connect(self.path)
            await self._db.executescript(SCHEMA)
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

    async def message(self, session_id: str, msg: MessageNew) -> None:
        await self._run(
            "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)",
            [(session_id, msg.message_id, msg.role, msg.position, msg.text, utc_now_iso())],
        )

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
