# -*- coding: utf-8 -*-
"""Small persistent usage/revenue ledger for the hosted service."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


class UsageMetrics:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    decision TEXT,
                    owner_hash TEXT,
                    agent_hash TEXT,
                    amount_usdc REAL NOT NULL DEFAULT 0,
                    detail_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_usage_kind ON usage_events(kind);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _identity(value: str | None) -> str | None:
        return hashlib.sha256(value.lower().encode()).hexdigest() if value else None

    def evaluation(self, verdict: dict, owner: str | None, agent_id: str | None,
                   tier: str = "basic"):
        with self._connect() as db:
            db.execute("INSERT INTO usage_events(ts,kind,decision,owner_hash,agent_hash,detail_json) VALUES (?,?,?,?,?,?)",
                       (time.time(), f"evaluation:{tier}", verdict.get("decision"),
                        self._identity(owner), self._identity(agent_id),
                        json.dumps({"layer": verdict.get("layer")})))

    def revenue(self, kind: str, amount_usdc: float):
        if kind not in ("paid_semantic", "free_semantic"):
            raise ValueError("unknown revenue event")
        with self._connect() as db:
            db.execute("INSERT INTO usage_events(ts,kind,amount_usdc) VALUES (?,?,?)",
                       (time.time(), kind, amount_usdc))

    def summary(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT kind,decision,COUNT(*) AS n,COALESCE(SUM(amount_usdc),0) AS amount FROM usage_events GROUP BY kind,decision").fetchall()
            unique_owners = db.execute("SELECT COUNT(DISTINCT owner_hash) FROM usage_events WHERE owner_hash IS NOT NULL").fetchone()[0]
            unique_agents = db.execute("SELECT COUNT(DISTINCT agent_hash) FROM usage_events WHERE agent_hash IS NOT NULL").fetchone()[0]
        evaluations = {"ALLOW": 0, "ASK": 0, "DENY": 0, "INDETERMINATE": 0}
        basic = semantic = paid = free = 0
        earned = 0.0
        for kind, decision, count, amount in rows:
            if kind.startswith("evaluation:") and decision in evaluations:
                evaluations[decision] += count
                if kind == "evaluation:basic":
                    basic += count
                elif kind == "evaluation:semantic":
                    semantic += count
            elif kind == "paid_semantic":
                paid += count
                earned += float(amount)
            elif kind == "free_semantic":
                free += count
        return {"evaluations_total": basic + semantic,
                "evaluations_by_decision": evaluations,
                "basic_evaluations": basic,
                "semantic_evaluations": semantic,
                "paid_semantic_calls": paid,
                "free_semantic_calls": free,
                "earned_usdc": round(earned, 6),
                "unique_owners": unique_owners,
                "unique_agents": unique_agents}
