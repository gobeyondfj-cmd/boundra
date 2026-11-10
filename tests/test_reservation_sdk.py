from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from boundra_x402 import BoundraClient, create_x402_hook
from gateway import Gateway
from policy import PaymentIntent
from spend_ledger import DuplicateReservation, ReservationError, SpendLedger
from usage_metrics import UsageMetrics


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LedgerTests(unittest.TestCase):
    def test_reservation_settle_release_and_atomic_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SpendLedger(Path(tmp) / "spend.db")
            first = ledger.reserve("0xOWNER", "bot", "api.example", 4, 5, 4)
            self.assertEqual(ledger.spent_today("0xowner"), 4)
            with self.assertRaises(ReservationError):
                ledger.reserve("0xOWNER", "bot", "api.example", 1, 5, 4)
            with self.assertRaises(PermissionError):
                ledger.release(first["evaluation_id"], "wrong-token")
            released = ledger.release(first["evaluation_id"], first["reservation_token"])
            self.assertEqual(released["status"], "released")
            self.assertEqual(ledger.spent_today("0xOWNER"), 0)
            second = ledger.reserve("0xOWNER", "bot", "api.example", 3, 5, 4)
            settled = ledger.settle(second["evaluation_id"], second["reservation_token"], "0xtx")
            self.assertEqual(settled["status"], "settled")
            self.assertEqual(settled["settlement_ref"], "0xtx")
            self.assertNotIn("token_hash", settled)
            self.assertNotIn("reservation_token", json.dumps(settled))

    def test_concurrent_reservations_cannot_overspend(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SpendLedger(Path(tmp) / "spend.db")
            def reserve():
                try:
                    ledger.reserve("owner", "bot", "api", 0.6, 1)
                    return True
                except ReservationError:
                    return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: reserve(), range(6)))
            self.assertEqual(sum(results), 1)
            self.assertAlmostEqual(ledger.spent_today("owner"), 0.6)

    def test_purchase_id_replay_conflict_and_independent_purchases(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SpendLedger(Path(tmp) / "spend.db")
            first = ledger.reserve("owner", "bot", "api", 0.6, 5,
                                   purchase_id="purchase-a", intent_digest=digest("same-quote"))
            with self.assertRaises(DuplicateReservation) as replay:
                ledger.reserve("OWNER", "bot", "api", 0.6, 5,
                               purchase_id="purchase-a", intent_digest=digest("same-quote"))
            self.assertFalse(replay.exception.conflict)
            self.assertEqual(replay.exception.state["id"], first["evaluation_id"])
            self.assertEqual(replay.exception.state["status"], "reserved")
            with self.assertRaises(DuplicateReservation) as conflict:
                ledger.reserve("owner", "bot", "api", 0.7, 5,
                               purchase_id="purchase-a", intent_digest=digest("changed-terms"))
            self.assertTrue(conflict.exception.conflict)
            second = ledger.reserve("owner", "bot", "api", 0.6, 5,
                                    purchase_id="purchase-b", intent_digest=digest("same-quote"))
            self.assertNotEqual(second["evaluation_id"], first["evaluation_id"])
            self.assertEqual(ledger.spent_today("owner"), 1.2)

    def test_concurrent_same_purchase_only_one_reservation_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SpendLedger(Path(tmp) / "spend.db")
            def reserve():
                try:
                    return ledger.reserve("owner", "bot", "api", 0.6, 5,
                                          purchase_id="shared-purchase",
                                          intent_digest=digest("identical-offer"))
                except DuplicateReservation as exc:
                    return exc
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: reserve(), range(8)))
            allowed = [r for r in results if isinstance(r, dict)]
            duplicates = [r for r in results if isinstance(r, DuplicateReservation)]
            self.assertEqual(len(allowed), 1)
            self.assertEqual(len(duplicates), 7)
            self.assertTrue(all(r.state["id"] == allowed[0]["evaluation_id"] for r in duplicates))
            self.assertEqual(ledger.spent_today("owner"), 0.6)

    def test_purchase_id_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SpendLedger(Path(tmp) / "spend.db")
            with self.assertRaises(ValueError):
                ledger.reserve("owner", "bot", "api", 1, 5, purchase_id="missing-digest")
            with self.assertRaises(ValueError):
                ledger.reserve("owner", "bot", "api", 1, 5,
                               purchase_id=" ", intent_digest=digest("intent"))

    def test_legacy_database_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spend.db"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE evaluations (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL, agent_id TEXT,
                        resource TEXT, day TEXT NOT NULL, amount REAL NOT NULL,
                        status TEXT NOT NULL, token_hash TEXT NOT NULL,
                        reserved_at REAL NOT NULL, expires_at REAL NOT NULL,
                        settled_at REAL, released_at REAL, settlement_ref TEXT
                    );
                    INSERT INTO evaluations VALUES
                    ('legacy','owner','bot','api','2026-01-01',0.25,'settled',
                     'hash',1,2,3,NULL,'0xlegacy');
                """)
            ledger = SpendLedger(path)
            legacy = ledger.get("legacy")
            self.assertEqual(legacy["amount"], 0.25)
            self.assertEqual(legacy["settlement_ref"], "0xlegacy")
            self.assertIsNone(legacy["purchase_id"])
            with sqlite3.connect(path) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
            self.assertIn("purchase_id", columns)
            self.assertIn("intent_digest", columns)


class GatewayPurchaseTests(unittest.TestCase):
    def test_duplicate_never_receives_a_second_signing_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = Gateway("max $10 per transaction; daily spend cap $50",
                              ledger=SpendLedger(Path(tmp) / "spend.db"))
            intent = PaymentIntent(amount=1, recipient="api", purpose="data")
            first = gateway.evaluate_payment(intent, owner="owner", purchase_id="purchase-a")
            replay = gateway.evaluate_payment(intent, owner="owner", purchase_id="purchase-a")
            conflict = gateway.evaluate_payment(
                PaymentIntent(amount=2, recipient="api", purpose="data"),
                owner="owner", purchase_id="purchase-a")
            independent = gateway.evaluate_payment(intent, owner="owner", purchase_id="purchase-b")
            self.assertEqual(first["decision"], "ALLOW")
            self.assertEqual(replay["decision"], "DENY")
            self.assertEqual(replay["layer"], "idempotency")
            self.assertTrue(replay["duplicate"])
            self.assertFalse(replay["purchase_id_conflict"])
            self.assertEqual(replay["evaluation_id"], first["evaluation_id"])
            self.assertNotIn("reservation_token", replay)
            self.assertEqual(conflict["decision"], "DENY")
            self.assertTrue(conflict["purchase_id_conflict"])
            self.assertEqual(independent["decision"], "ALLOW")
            self.assertEqual(gateway.spent_today("owner"), 2)


class MetricsTests(unittest.TestCase):
    def test_metrics_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            metrics = UsageMetrics(path)
            metrics.evaluation({"decision": "ALLOW", "layer": "pass"}, "Owner", "Bot")
            metrics.revenue("paid_semantic", 0.003)
            summary = UsageMetrics(path).summary()
            self.assertEqual(summary["evaluations_total"], 1)
            self.assertEqual(summary["paid_semantic_calls"], 1)
            self.assertEqual(summary["earned_usdc"], 0.003)
            self.assertEqual(summary["unique_owners"], 1)
            self.assertNotIn("Owner", json.dumps(summary))


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_settle_release_and_hook(self):
        seen = []
        def handler(request: httpx.Request):
            seen.append(request)
            if request.url.path == "/evaluate/basic":
                return httpx.Response(200, json={"decision": "ALLOW", "evaluation_id": "e1"})
            return httpx.Response(200, json={"id": "e1", "status": "settled"})
        transport = httpx.MockTransport(handler)
        async with BoundraClient("https://api.test", owner="0xowner", agent_id="bot", transport=transport) as client:
            verdict = await client.evaluate(
                {"amount": 1, "recipient": "api", "purpose": "data"},
                purchase_id="purchase-a")
            self.assertEqual(verdict["decision"], "ALLOW")
            state = await client.settle("e1", "secret", "0xhash")
            self.assertEqual(state["status"], "settled")
            ctx = SimpleNamespace(
                selected_requirements=SimpleNamespace(amount="1000000", payTo="0xpayee", network="eip155:8453", asset="USDC"),
                payment_required={"resource": {"url": "https://api.test/data", "method": "GET", "description": "data"}})
            # Python x402 uses pay_to; make the fixture match that actual SDK contract.
            ctx.selected_requirements.pay_to = ctx.selected_requirements.payTo
            hook = create_x402_hook(client, purpose="data", expected_amount=1,
                                    purchase_id="purchase-b")
            self.assertIsNone(await hook(ctx))
        body = json.loads(seen[0].content)
        self.assertEqual(body["owner"], "0xowner")
        self.assertEqual(body["agent_id"], "bot")
        self.assertEqual(body["purchase_id"], "purchase-a")
        self.assertEqual(json.loads(seen[2].content)["purchase_id"], "purchase-b")
        self.assertNotIn("secret", seen[0].content.decode())

    async def test_network_failure_aborts_before_signing(self):
        transport = httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("offline")))
        async with BoundraClient("https://api.test", transport=transport) as client:
            hook = create_x402_hook(client, purpose="data")
            ctx = SimpleNamespace(
                selected_requirements=SimpleNamespace(amount="1000", pay_to="0xpayee"),
                payment_required={})
            result = await hook(ctx)
            from x402.schemas.hooks import AbortResult
            self.assertIsInstance(result, AbortResult)
            self.assertIn("failed closed", result.message)


if __name__ == "__main__":
    unittest.main()
