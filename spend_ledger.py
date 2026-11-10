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


class DuplicateReservation(ReservationError):
    """A purchase ID already has a reservation; a second ALLOW is unsafe."""

    def __init__(self, message: str, state: dict, *, conflict: bool = False):
        super().__init__(message)
        self.state = state
        self.conflict = conflict


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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(evaluations)")}
            if "purchase_id" not in columns:
                db.execute("ALTER TABLE evaluations ADD COLUMN purchase_id TEXT")
            if "intent_digest" not in columns:
                db.execute("ALTER TABLE evaluations ADD COLUMN intent_digest TEXT")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_purchase
                ON evaluations(owner, purchase_id) WHERE purchase_id IS NOT NULL
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
    def _validate_purchase(purchase_id: str | None, intent_digest: str | None):
        if purchase_id is None:
            return
        if (not isinstance(purchase_id, str) or not purchase_id
                or purchase_id != purchase_id.strip() or len(purchase_id) > 128):
            raise ValueError("purchase_id must be a non-empty trimmed string of at most 128 characters")
        if (not isinstance(intent_digest, str) or len(intent_digest) != 64):
            raise ValueError("a SHA-256 intent_digest is required with purchase_id")
        try:
            bytes.fromhex(intent_digest)
        except ValueError as exc:
            raise ValueError("intent_digest must be a hexadecimal SHA-256 digest") from exc

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
                resource_daily_cap: float | None = None,
                purchase_id: str | None = None,
                intent_digest: str | None = None) -> dict:
        self._validate_purchase(purchase_id, intent_digest)
        evaluation_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        now = time.time()
        today = date.today().isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._expire(db, now)
                if purchase_id is not None:
                    row = db.execute(
                        "SELECT * FROM evaluations WHERE owner=? AND purchase_id=?",
                        (owner.lower(), purchase_id)).fetchone()
                    if row:
                        state = self._public(dict(row))
                        if not secrets.compare_digest(row["intent_digest"] or "", intent_digest or ""):
                            raise DuplicateReservation(
                                "purchase_id conflict: the ID is already bound to a different intent",
                                state, conflict=True)
                        raise DuplicateReservation(
                            f"purchase_id already {row['status']}; duplicate signing permission denied",
                            state)
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
                    (id,owner,agent_id,resource,day,amount,status,token_hash,reserved_at,expires_at,
                     purchase_id,intent_digest)
                    VALUES (?,?,?,?,?,?, 'reserved', ?,?,?,?,?)
                """, (evaluation_id, owner.lower(), (agent_id or "").lower() or None,
                      resource, today, amount, self._hash(token), now,
                      now + self.reservation_ttl_seconds, purchase_id, intent_digest))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {"evaluation_id": evaluation_id, "reservation_token": token,
                "reservation_status": "reserved", "purchase_id": purchase_id,
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
                 "reserved_at", "expires_at", "settled_at", "released_at", "settlement_ref",
                 "purchase_id")}

    def get(self, evaluation_id: str) -> dict | None:
        with self._connect() as db:
            self._expire(db, time.time())
            row = db.execute("SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
            return self._public(dict(row)) if row else None
