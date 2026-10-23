# -*- coding: utf-8 -*-
"""
mcp_server.py — 支付审批网关的标准 MCP server（stdio）v3

工具分三类：
  · Agent 日常：evaluate_payment（支持批准令牌与 Mandate）/ get_policy /
    get_spend_report / get_audit_trail / request_policy_change
  · 用户专属（特权，需钱包签名）：set_policy / revoke_policy
  · 定制辅助：list_templates / render_template

人在回路：ASK 返回 challenge → 用户设备签名（approval.sign_approval）
→ Agent 持令牌重调 evaluate_payment(approval_token=...) → 验签放行。
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).parent))
from gateway import AUDIT_LOG, Gateway
from signed_policy import PolicyDocument, Revocation

DEFAULT_RULE = ("Daily spend cap $50 across all agents, max $10 per transaction; "
                "only pay whitelisted providers (api.weather.com, data.news.io); "
                "any payment above $5 requires my approval")

rule_file = Path(__file__).parent / "policy_rule.txt"
NL_RULE = rule_file.read_text(encoding="utf-8").strip() if rule_file.exists() else DEFAULT_RULE

gateway = Gateway(NL_RULE)

# Agent 策略变更申请（内存队列；生产应持久化 + 推送用户）
policy_requests: list[dict] = []

server = MCPServer(
    "boundra",
    instructions=("Boundra — payment approval gateway for AI agents. You MUST call "
                  "evaluate_payment before any payment: ALLOW = proceed, DENY = abandon, "
                  "ASK = relay the reason to the user and retry with approval_token after "
                  "the user signs the challenge. Policies change only via the user's "
                  "signature (set_policy/revoke_policy); agents may only file requests "
                  "via request_policy_change."),
)


# ---------------------------------------------------------------- Agent 日常

@server.tool(description=(
    "Evaluate a payment intent. MUST be called before executing any payment. "
    "ALLOW = you may pay; DENY = you must abandon; ASK = a challenge is returned — "
    "relay the reason to the user, then retry with approval_token once the user signs. "
    "An optional mandate parameter carries a user-issued single-task authorization."))
def evaluate_payment(amount: float, recipient: str, purpose: str,
                     expected_amount: float | None = None,
                     source_content: str = "",
                     approval_token: str | None = None,
                     owner: str | None = None,
                     agent_id: str | None = None,
                     mandate: dict | None = None,
                     human_approved: bool = False) -> str:   # deprecated，仅向后兼容
    verdict = gateway.handle_tool_call("evaluate_payment", {
        "intent": {"amount": amount, "recipient": recipient, "purpose": purpose,
                   "expected_amount": expected_amount, "source_content": source_content},
        "approval_token": approval_token, "owner": owner,
        "agent_id": agent_id, "mandate": mandate,
        "human_approved": human_approved,
    })
    return json.dumps(verdict, ensure_ascii=False)


@server.tool(description=(
    "[For agents] Ask the user for a policy/budget change (e.g. 'raise daily budget from 50 to 80'). "
    "The request is queued for user approval; the user signs and publishes a new policy version. "
    "Agents can NEVER change policy directly."))
def request_policy_change(agent_id: str, request: str, reason: str) -> str:
    req = {"id": uuid.uuid4().hex[:10], "agent_id": agent_id, "request": request,
           "reason": reason, "ts": time.time(), "status": "pending"}
    policy_requests.append(req)
    return json.dumps({"submitted": True, "request_id": req["id"],
                       "note": "queued for user approval"}, ensure_ascii=False)


@server.tool(description="View the currently active payment policy (legacy fallback rule)")
def get_policy() -> str:
    legacy = gateway.legacy_policy
    if not legacy:
        return json.dumps({"note": "no legacy rule — only signed policies are in effect"}, ensure_ascii=False)
    return json.dumps({"raw_rule": legacy.raw_mandate,
                       **gateway.policy_summary}, ensure_ascii=False)


@server.tool(description="View today's spend summary")
def get_spend_report(owner: str | None = None) -> str:
    today = date.today().isoformat()
    spent = gateway.spent_today(owner)
    budget = gateway.legacy_policy.daily_budget if gateway.legacy_policy else None
    return json.dumps({"date": today, "owner": owner, "spent": round(spent, 2),
                       "daily_budget": budget,
                       "remaining": round(budget - spent, 2) if budget else None},
                      ensure_ascii=False)


@server.tool(description="View recent payment-approval audit records")
def get_audit_trail(limit: int = 10) -> str:
    if not AUDIT_LOG.exists():
        return "[]"
    lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    return json.dumps([json.loads(x) for x in lines], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 用户专属（特权）

def _compiled(nl_rule: str) -> dict:
    from policy import compile_policy
    c = compile_policy(nl_rule)
    return {"per_tx_max": c.per_tx_max, "daily_budget": c.daily_budget,
            "allowed_recipients": c.allowed_recipients,
            "require_approval_above": c.require_approval_above}


@server.tool(description=(
    "[USER ONLY] Submit a new policy version. Must carry the user's wallet EIP-191 signature "
    "over the policy document; accepted only after signature + version-chain checks pass. "
    "Agents MUST NOT call this tool."))
def set_policy(owner: str, scope: str, version: int, nl_rule: str,
               prev_hash: str, created_at: float, signature: str,
               agent_id: str | None = None) -> str:
    doc = PolicyDocument(owner=owner, scope=scope, agent_id=agent_id, version=version,
                         nl_rule=nl_rule, compiled=_compiled(nl_rule),
                         prev_hash=prev_hash, created_at=created_at,
                         signature=signature)
    doc.finalize()
    try:
        result = gateway.set_policy(doc)
    except (PermissionError, ValueError) as e:
        return json.dumps({"accepted": False, "error": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@server.tool(description=(
    "[USER ONLY] Revoke a policy chain (takes effect immediately; subsequent payments "
    "default to DENY). Must carry the user's wallet signature over the revocation statement."))
def revoke_policy(owner: str, scope: str, revoked_through: int,
                  created_at: float, signature: str,
                  agent_id: str | None = None) -> str:
    rec = Revocation(owner=owner, scope=scope, agent_id=agent_id,
                     revoked_through=revoked_through, created_at=created_at,
                     signature=signature)
    try:
        result = gateway.revoke_policy(rec)
    except PermissionError as e:
        return json.dumps({"revoked": False, "error": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@server.tool(description="View a user's policy version history (version chain + hashes, verifiable against on-chain commitments)")
def get_policy_versions(owner: str) -> str:
    return json.dumps(gateway.store.history(owner), ensure_ascii=False, indent=2)


@server.tool(description="List pending agent policy-change requests")
def list_policy_requests() -> str:
    return json.dumps(policy_requests, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 定制辅助

@server.tool(description="List policy templates (personal assistant / travel / dev API / procurement)")
def list_templates() -> str:
    import templates
    return json.dumps(templates.list_templates(), ensure_ascii=False, indent=2)


@server.tool(description="Render a natural-language rule from a template + parameters (then compile, sign & publish)")
def render_template(template_id: str, whitelist: list[str],
                    daily: float | None = None, per_tx: float | None = None,
                    ask_above: float | None = None) -> str:
    import templates
    overrides = {k: v for k, v in {"daily": daily, "per_tx": per_tx,
                                   "ask_above": ask_above}.items() if v is not None}
    try:
        rule = templates.render_template(template_id, whitelist, **overrides)
    except KeyError:
        return json.dumps({"error": f"unknown template: {template_id}"}, ensure_ascii=False)
    return json.dumps({"nl_rule": rule}, ensure_ascii=False)


def main() -> None:
    server.run()   # stdio 传输


if __name__ == "__main__":
    main()
