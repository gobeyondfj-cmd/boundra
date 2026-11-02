from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from approval import sign_approval
from eth_account import Account
from gateway import Gateway
from policy import PaymentIntent, compile_policy, evaluate
from signed_policy import LocalAnchor, PolicyStore, new_document, sign_document
from spend_ledger import ReservationError, SpendLedger

VECTORS = json.loads((Path(__file__).parent / "vectors" / "policy_consistency.json").read_text())


class ConsistencyVectors(unittest.TestCase):
    def test_policy_vectors(self):
        policy = compile_policy(VECTORS["rule"])
        for vector in (v for v in VECTORS["vectors"] if v["category"] == "policy"):
            with self.subTest(vector=vector["id"]):
                decision = evaluate(policy, PaymentIntent(**vector["intent"]),
                                    vector.get("spent_today", 0),
                                    vector.get("resource_spent_today", 0))
                self.assertEqual(decision.action, vector["expect"]["decision"])
                self.assertEqual(decision.layer, vector["expect"]["layer"])
                if vector["id"].startswith("P-03"):
                    self.assertNotIn("malicious", " ".join(decision.reasons).lower())

    def test_ledger_vectors(self):
        for vector in (v for v in VECTORS["vectors"] if v["category"] == "ledger"):
            with self.subTest(vector=vector["id"]), tempfile.TemporaryDirectory() as tmp:
                ledger = SpendLedger(Path(tmp) / "spend.db")
                saved = {}
                for operation in vector["operations"]:
                    op = operation["op"]
                    if op == "reserve":
                        try:
                            result = ledger.reserve("owner", "bot", operation["resource"],
                                                    operation["amount"], vector["daily_cap"],
                                                    vector["resource_cap"])
                            self.assertEqual(result["reservation_status"], operation["expect"])
                            saved[operation["save"]] = result
                        except ReservationError:
                            self.assertEqual(operation["expect"], "DENY")
                    elif op == "release":
                        item = saved[operation["ref"]]
                        result = ledger.release(item["evaluation_id"], item["reservation_token"])
                        self.assertEqual(result["status"], operation["expect"])
                    elif op == "settle":
                        item = saved[operation["ref"]]
                        result = ledger.settle(item["evaluation_id"], item["reservation_token"],
                                               operation["settlement_ref"])
                        self.assertEqual(result["status"], operation["expect"])
                    elif op == "spent":
                        self.assertAlmostEqual(ledger.spent_today("owner"), operation["expect"])

    def test_approval_vectors(self):
        for vector in (v for v in VECTORS["vectors"] if v["category"] == "approval"):
            with self.subTest(vector=vector["id"]), tempfile.TemporaryDirectory() as tmp:
                account = Account.create()
                store = PolicyStore(LocalAnchor(Path(tmp) / "anchors.jsonl"))
                gateway = Gateway(store=store, ledger=SpendLedger(Path(tmp) / "spend.db"))
                doc = new_document(account.address, "user", None, 1, vector["rule"], "0" * 64)
                doc.signature = sign_document(doc, account.key.hex())
                gateway.set_policy(doc)
                intent = PaymentIntent(**vector["intent"])
                first = gateway.evaluate_payment(intent, owner=account.address, agent_id="bot")
                self.assertEqual(first["decision"], vector["expect"]["initial"])
                token = sign_approval(first["challenge"], account.key.hex())
                approved = gateway.evaluate_payment(intent, owner=account.address,
                                                    agent_id="bot", approval_token=token)
                self.assertEqual(approved["decision"], vector["expect"]["approved"])
                replay = gateway.evaluate_payment(intent, owner=account.address,
                                                  agent_id="bot", approval_token=token)
                self.assertEqual(replay["decision"], vector["expect"]["replayed"])


if __name__ == "__main__":
    unittest.main()
    print(f"Validated {len(VECTORS['vectors'])} CC0 consistency vectors")
