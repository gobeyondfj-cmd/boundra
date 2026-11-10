# -*- coding: utf-8 -*-
"""
gateway.py — 支付审批网关（策略决策点 PDP）v3

四层策略叠加（下层只能收紧，取最严裁决）：
  第 0 层  平台红线（内置：注入一票否决 + 绝对限额，任何人不可放宽）
  第 1 层  用户策略（签名文档，版本链）
  第 2 层  Agent 策略（签名文档，绑定 agent_id）
  第 3 层  任务 Mandate（单次授权，用完即焚，可选）

人工审批：ASK 时返回 challenge；用户在自己设备签名 → 批准令牌；
Agent 持令牌重调，网关验签 + 意图逐字段比对 + nonce 防重放。
legacy human_approved 自证模式保留仅为向后兼容，裁决中标记 deprecated。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import approval as approval_mod
from mandate import Mandate, check_against_intent, verify_mandate
from policy import (Decision, PaymentIntent, Policy, compile_policy, evaluate,
                    platform_check)
from signed_policy import PolicyDocument, PolicyStore, Revocation
from spend_ledger import DuplicateReservation, ReservationError, SpendLedger

STATE_DIR = Path(os.environ.get("BOUNDRA_STATE_DIR") or (Path(__file__).parent / "state"))
AUDIT_LOG = STATE_DIR / "audit.jsonl"

_SEVERITY = {"INDETERMINATE": 3, "DENY": 2, "ASK": 1, "ALLOW": 0}


class Gateway:
    def __init__(self, nl_rule: str | None = None,
                 human_approval_handler: Optional[Callable[[PaymentIntent, Decision], bool]] = None,
                 store: PolicyStore | None = None,
                 verdict_signer=None, ledger: SpendLedger | None = None):
        self.legacy_policy: Policy | None = compile_policy(nl_rule) if nl_rule else None
        self.human_approval_handler = human_approval_handler
        self.store = store or PolicyStore()
        # 可选的平台凭证签名器（verdict_credential.VerdictSigner）；
        # 设置后每次评估的 verdict 附带 EIP-191 签名凭证
        self.verdict_signer = verdict_signer
        self.ledger = ledger
        # 无持久化 ledger 的本地兼容路径；托管环境应始终注入 SpendLedger。
        self._spent: dict[str, float] = {}
        self._used_tokens: set = set()        # 批准令牌 nonce 防重放
        self._used_mandates: set = set()      # Mandate 单次消费
        STATE_DIR.mkdir(exist_ok=True)

    @staticmethod
    def _spent_key(owner: str | None, today: str) -> str:
        return f"{(owner or 'local').lower()}:{today}"

    def spent_today(self, owner: str | None = None, agent_id: str | None = None,
                    resource: str | None = None) -> float:
        if self.ledger and owner:
            return self.ledger.spent_today(owner, agent_id, resource)
        today = date.today().isoformat()
        return self._spent.get(self._spent_key(owner, today), 0.0)

    # ---- 策略写入（特权操作，仅验签通过才生效） ------------------------
    def set_policy(self, doc: PolicyDocument) -> dict:
        accepted = self.store.submit(doc)
        return {"accepted": True, "version": accepted.version, "hash": accepted.hash,
                "scope": accepted.scope, "agent_id": accepted.agent_id}

    def revoke_policy(self, rec: Revocation) -> dict:
        self.store.revoke(rec)
        return {"revoked": True, "scope": rec.scope, "agent_id": rec.agent_id,
                "revoked_through": rec.revoked_through}

    # ---- 策略解析 ----------------------------------------------------
    def _resolve_policies(self, owner: str | None, agent_id: str | None):
        policies, refs = [], []
        if owner:
            # 撤销检查：任一相关链被撤销 → 硬拒绝由调用方处理
            for scope, aid in (("agent", agent_id), ("user", None)):
                if scope == "agent" and not agent_id:
                    continue
                if self.store.is_revoked(owner, scope, aid):
                    raise PermissionError(f"policy {scope}/{aid or 'all'} revoked by the user — default-deny")
                doc = self.store.active(owner, scope, aid)
                if not doc:
                    continue
                problems = self.store.check_integrity(doc)
                if problems:
                    raise PermissionError(f"policy {scope}/v{doc.version} integrity check failed: "
                                          f"{'; '.join(problems)}. Refusing to act on an untrusted policy.")
                policies.append(doc.to_policy())
                refs.append({"scope": scope, "version": doc.version, "hash": doc.hash})
        if not policies and self.legacy_policy:
            policies.append(self.legacy_policy)
            refs.append({"scope": "legacy", "version": None, "hash": None})
        return policies, refs

    # ---- MCP tool 形态的统一入口 -------------------------------------
    def handle_tool_call(self, tool: str, args: dict) -> dict:
        if tool != "evaluate_payment":
            return {"error": f"unknown tool: {tool}"}
        intent = PaymentIntent(**args["intent"])
        return self.evaluate_payment(
            intent, mandate_context=args.get("context", {}),
            human_approved=args.get("human_approved", False),
            owner=args.get("owner"), agent_id=args.get("agent_id"),
            approval_token=args.get("approval_token"),
            mandate=args.get("mandate"), purchase_id=args.get("purchase_id"))

    # ---- 核心：评估一笔支付意图 --------------------------------------
    def evaluate_payment(self, intent: PaymentIntent, mandate_context: dict | None = None,
                         human_approved: bool = False,
                         owner: str | None = None, agent_id: str | None = None,
                         approval_token: str | None = None,
                         mandate: dict | None = None,
                         purchase_id: str | None = None) -> dict:
        today = date.today().isoformat()
        spent_key = self._spent_key(owner, today)
        spent = self.spent_today(owner)
        resource_spent = (self.spent_today(owner, resource=intent.resource)
                          if intent.resource else 0.0)
        # Approval/mandate compatibility remains bound to the original three fields.
        intent_d = {"amount": intent.amount, "recipient": intent.recipient,
                    "purpose": intent.purpose}

        if not math.isfinite(intent.amount) or intent.amount <= 0 or not intent.recipient.strip():
            d = Decision("INDETERMINATE", layer="input",
                         reasons=["amount must be finite and positive, and recipient must be present"])
            return self._finalize(d, d.action, None, intent, agent_id, owner,
                                  [], spent, today)

        # 第 0 层：平台红线（先于一切）
        red = platform_check(intent, spent)
        if red:
            return self._finalize(red, red.action, None, intent, agent_id, owner,
                                  [], spent, today)

        # 第 1+2 层：用户 / Agent 签名策略
        try:
            policies, policy_refs = self._resolve_policies(owner, agent_id)
        except PermissionError as e:
            d = Decision("DENY", layer="policy", reasons=[str(e)])
            return self._finalize(d, "DENY", None, intent, agent_id, owner,
                                  [], spent, today)
        if not policies:
            d = Decision("DENY", layer="policy",
                         reasons=["no (signed) policy found — default-deny"])
            return self._finalize(d, "DENY", None, intent, agent_id, owner,
                                  [], spent, today)

        decisions = [evaluate(p, intent, spent, resource_spent) for p in policies]
        decision = max(decisions, key=lambda d: _SEVERITY[d.action])
        if len(decisions) > 1:
            decision.reasons.append(f"(stacked {len(decisions)} policy layers, strictest wins)")

        # 第 3 层：任务 Mandate（可选，单次消费）
        mandate_id = None
        if mandate:
            m = Mandate(**mandate)
            ok, why = verify_mandate(m)
            if ok and m.mandate_id in self._used_mandates:
                ok, why = False, "mandate already used (single-use authorization)"
            if ok and owner and m.owner.lower() != owner.lower():
                ok, why = False, "mandate owner does not match this evaluation's owner"
            if ok:
                ok, why = check_against_intent(m, intent_d)
            if not ok:
                d = Decision("DENY", layer="mandate", reasons=[f"mandate check failed: {why}"])
                return self._finalize(d, "DENY", None, intent, agent_id, owner,
                                      policy_refs, spent, today)
            mandate_id = m.mandate_id
            # Mandate 通过 ≠ 直接放行：长期策略的 DENY/ASK 依然生效（取最严）

        # ---- 人工审批解析 ---------------------------------------------
        final_action, human_said, challenge = decision.action, None, None
        if decision.action == "ASK":
            if approval_token and owner:
                ok, why = approval_mod.verify_approval(
                    approval_token, intent_d, owner, self._used_tokens)
                if ok:
                    final_action, human_said = "ALLOW", "approve(token)"
                else:
                    final_action = "DENY"
                    decision.reasons.append(f"invalid approval token: {why}")
            elif human_approved:
                # legacy 自证模式：仅向后兼容，生产必须废弃
                final_action, human_said = "ALLOW", "approve(legacy-deprecated)"
            elif self.human_approval_handler:
                approved = self.human_approval_handler(intent, decision)
                human_said = "approve" if approved else "reject"
                final_action = "ALLOW" if approved else "DENY"
            else:
                final_action = "ASK"
                challenge = approval_mod.make_challenge(intent_d, policy_refs)

        reservation = None
        if final_action == "ALLOW":
            if self.ledger and owner:
                daily_cap = min([p.daily_budget for p in policies] + [5000.0])
                resource_caps = [p.per_resource_daily_budget for p in policies
                                 if p.per_resource_daily_budget is not None]
                intent_digest = None
                if purchase_id is not None:
                    canonical = json.dumps(intent.__dict__, sort_keys=True,
                                           separators=(",", ":"), ensure_ascii=False)
                    intent_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                try:
                    reservation = self.ledger.reserve(
                        owner=owner, agent_id=agent_id,
                        resource=intent.resource or intent.recipient,
                        amount=intent.amount, daily_cap=daily_cap,
                        resource_daily_cap=min(resource_caps) if resource_caps and intent.resource else None,
                        purchase_id=purchase_id, intent_digest=intent_digest)
                except DuplicateReservation as e:
                    final_action = "DENY"
                    decision.layer = "idempotency"
                    decision.reasons.append(str(e))
                    reservation = {
                        "evaluation_id": e.state["id"],
                        "reservation_status": e.state["status"],
                        "reservation_expires_at": e.state["expires_at"],
                        "purchase_id": e.state["purchase_id"],
                        "duplicate": True,
                        "purchase_id_conflict": e.conflict,
                    }
                except ReservationError as e:
                    final_action = "DENY"
                    decision.layer = "budget"
                    decision.reasons.append(str(e))
                except ValueError as e:
                    final_action = "INDETERMINATE"
                    decision.layer = "input"
                    decision.reasons.append(str(e))
            else:
                self._spent[spent_key] = spent + intent.amount
            if final_action == "ALLOW" and mandate_id:
                self._used_mandates.add(mandate_id)   # 单次消费

        return self._finalize(decision, final_action, human_said, intent,
                              agent_id, owner, policy_refs, spent, today,
                              mandate_id=mandate_id, challenge=challenge,
                              reservation=reservation, purchase_id=purchase_id)

    # ---- 收尾：计账 + 审计 + 返回 ------------------------------------
    def _finalize(self, decision: Decision, final_action: str, human_said,
                  intent: PaymentIntent, agent_id, owner, policy_refs,
                  spent: float, today: str, mandate_id=None, challenge=None,
                  reservation: dict | None = None,
                  purchase_id: str | None = None) -> dict:
        record_intent = {"amount": intent.amount, "recipient": intent.recipient,
                         "purpose": intent.purpose}
        optional_intent = {
            "expected_amount": intent.expected_amount,
            "expected_recipient": intent.expected_recipient,
            "resource": intent.resource, "method": intent.method,
            "network": intent.network, "asset": intent.asset,
        }
        record_intent.update({k: v for k, v in optional_intent.items() if v not in (None, "")})
        record = {
            "id": uuid.uuid4().hex[:12],
            "ts": datetime.now().isoformat(timespec="seconds"),
            "intent": record_intent,
            "agent_id": agent_id, "owner": owner,
            "layer": decision.layer,
            "raw_decision": decision.action,
            "human": human_said,
            "final": final_action,
            "reasons": decision.reasons,
            "policy_refs": policy_refs,
            "mandate_id": mandate_id,
            "purchase_id": purchase_id,
            "evaluation_id": (reservation or {}).get("evaluation_id"),
            "reservation_status": (reservation or {}).get("reservation_status"),
            "duplicate": (reservation or {}).get("duplicate", False),
            "spent_today_after": round(self.spent_today(owner), 6),
        }
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        out = {"decision": final_action, "raw": decision.action,
               "layer": decision.layer, "reasons": decision.reasons,
               "human": human_said, "audit_id": record["id"],
               "policy_refs": policy_refs, "mandate_id": mandate_id,
               "spent_today": record["spent_today_after"]}
        if purchase_id is not None:
            out["purchase_id"] = purchase_id
        if reservation:
            out.update({"evaluation_id": reservation["evaluation_id"],
                        "reservation_status": reservation["reservation_status"],
                        "reservation_expires_at": reservation["reservation_expires_at"]})
            if reservation.get("duplicate"):
                out["duplicate"] = True
                out["purchase_id_conflict"] = reservation["purchase_id_conflict"]
        if challenge:
            out["challenge"] = challenge   # ASK：交用户签名的 challenge
        if self.verdict_signer is not None:
            # 平台签名凭证：任何第三方可离线验证这个判定确实发生
            out["credential"] = self.verdict_signer.sign(out, record["intent"], owner)
        if reservation and reservation.get("reservation_token"):
            # Secret capability token stays outside the signed/public credential and audit log.
            out["reservation_token"] = reservation["reservation_token"]
        return out

    def settle_evaluation(self, evaluation_id: str, reservation_token: str,
                          settlement_ref: str) -> dict:
        if not self.ledger:
            raise RuntimeError("persistent ledger is not configured")
        return self.ledger.settle(evaluation_id, reservation_token, settlement_ref)

    def release_evaluation(self, evaluation_id: str, reservation_token: str) -> dict:
        if not self.ledger:
            raise RuntimeError("persistent ledger is not configured")
        return self.ledger.release(evaluation_id, reservation_token)

    @property
    def policy_summary(self) -> dict:
        p = self.legacy_policy
        if not p:
            return {}
        return {"per_tx_max": p.per_tx_max, "daily_budget": p.daily_budget,
                "allowed_recipients": p.allowed_recipients,
                "require_approval_above": p.require_approval_above}
