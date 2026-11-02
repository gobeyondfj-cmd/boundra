# -*- coding: utf-8 -*-
"""Persistent, atomic spend reservations and settlement reconciliation.

ALLOW reserves budget; a later settlement confirms it and a failed/abandoned
payment releases it. The reservation token is returned only to the caller and
is never included in audit records or verdict credentials.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path


class ReservationError(RuntimeError):
    pass


class SpendLedger:
    def __init__(self, path: str | Path, reservation_ttl_seconds: int = 900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reservation_ttl_seconds = reservation_ttl_seconds
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    agent_id TEXT,
                    resource TEXT,
                    day TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    reserved_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    settled_at REAL,
                    released_at REAL,
                    settlement_ref TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_eval_budget
                    ON evaluations(owner, day, status);
                CREATE INDEX IF NOT EXISTS idx_eval_resource
                    ON evaluations(owner, resource, day, status);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _expire(db, now: float):
        db.execute("UPDATE evaluations SET status='expired' WHERE status='reserved' AND expires_at<=?", (now,))

    @staticmethod
    def _sum(db, owner: str, day: str, agent_id: str | None = None,
             resource: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount),0) FROM evaluations WHERE owner=? AND day=? AND status IN ('reserved','settled')"
        args: list = [owner.lower(), day]
        if agent_id is not None:
            sql += " AND agent_id=?"
            args.append(agent_id.lower())
        if resource is not None:
            sql += " AND resource=?"
            args.append(resource)
        return float(db.execute(sql, args).fetchone()[0])

    def spent_today(self, owner: str, agent_id: str | None = None,
                    resource: str | None = None) -> float:
        today = date.today().isoformat()
        with self._connect() as db:
            self._expire(db, time.time())
            return self._sum(db, owner, today, agent_id, resource)

    def reserve(self, owner: str, agent_id: str | None, resource: str | None,
                amount: float, daily_cap: float,
                resource_daily_cap: float | None = None) -> dict:
        evaluation_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        now = time.time()
        today = date.today().isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._expire(db, now)
                spent = self._sum(db, owner, today)
                if spent + amount > daily_cap:
                    raise ReservationError(
                        f"atomic budget guard: ${spent + amount:.6f} would exceed daily cap ${daily_cap:.6f}")
                if resource and resource_daily_cap is not None:
                    resource_spent = self._sum(db, owner, today, resource=resource)
                    if resource_spent + amount > resource_daily_cap:
                        raise ReservationError(
                            f"atomic resource budget guard: ${resource_spent + amount:.6f} "
                            f"would exceed per-resource daily cap ${resource_daily_cap:.6f}")
                db.execute("""
                    INSERT INTO evaluations
                    (id,owner,agent_id,resource,day,amount,status,token_hash,reserved_at,expires_at)
                    VALUES (?,?,?,?,?,?, 'reserved', ?,?,?)
                """, (evaluation_id, owner.lower(), (agent_id or "").lower() or None,
                      resource, today, amount, self._hash(token), now,
                      now + self.reservation_ttl_seconds))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {"evaluation_id": evaluation_id, "reservation_token": token,
                "reservation_status": "reserved",
                "reservation_expires_at": now + self.reservation_ttl_seconds}

    def _transition(self, evaluation_id: str, token: str, target: str,
                    settlement_ref: str | None = None) -> dict:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
                if not row or row["token_hash"] != self._hash(token):
                    raise PermissionError("invalid evaluation or reservation token")
                if row["status"] == target:
                    result = dict(row)
                    db.execute("COMMIT")
                    return self._public(result)
                if row["status"] != "reserved" and not (target == "settled" and row["status"] == "expired"):
                    raise ReservationError(f"cannot move evaluation from {row['status']} to {target}")
                if target == "settled":
                    db.execute("UPDATE evaluations SET status='settled',settled_at=?,settlement_ref=? WHERE id=?",
                               (now, settlement_ref, evaluation_id))
                else:
                    db.execute("UPDATE evaluations SET status='released',released_at=? WHERE id=?",
                               (now, evaluation_id))
                row = db.execute("SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
                result = dict(row)
                db.execute("COMMIT")
                return self._public(result)
            except Exception:
                db.execute("ROLLBACK")
                raise

    def settle(self, evaluation_id: str, token: str, settlement_ref: str) -> dict:
        if not settlement_ref:
            raise ValueError("settlement_ref is required")
        return self._transition(evaluation_id, token, "settled", settlement_ref)

    def release(self, evaluation_id: str, token: str) -> dict:
        return self._transition(evaluation_id, token, "released")

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: row.get(k) for k in
                ("id", "owner", "agent_id", "resource", "amount", "status",
                 "reserved_at", "expires_at", "settled_at", "released_at", "settlement_ref")}

    def get(self, evaluation_id: str) -> dict | None:
        with self._connect() as db:
            self._expire(db, time.time())
            row = db.execute("SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
            return self._public(dict(row)) if row else None
