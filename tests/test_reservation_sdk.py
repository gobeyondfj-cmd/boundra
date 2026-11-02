from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from boundra_x402 import BoundraClient, create_x402_hook
from spend_ledger import ReservationError, SpendLedger
from usage_metrics import UsageMetrics


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
            import concurrent.futures
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
            verdict = await client.evaluate({"amount": 1, "recipient": "api", "purpose": "data"})
            self.assertEqual(verdict["decision"], "ALLOW")
            state = await client.settle("e1", "secret", "0xhash")
            self.assertEqual(state["status"], "settled")
            ctx = SimpleNamespace(
                selected_requirements=SimpleNamespace(amount="1000000", payTo="0xpayee", network="eip155:8453", asset="USDC"),
                payment_required={"resource": {"url": "https://api.test/data", "method": "GET", "description": "data"}})
            # Python x402 uses pay_to; make the fixture match that actual SDK contract.
            ctx.selected_requirements.pay_to = ctx.selected_requirements.payTo
            hook = create_x402_hook(client, purpose="data", expected_amount=1)
            self.assertIsNone(await hook(ctx))
        body = json.loads(seen[0].content)
        self.assertEqual(body["owner"], "0xowner")
        self.assertEqual(body["agent_id"], "bot")
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
