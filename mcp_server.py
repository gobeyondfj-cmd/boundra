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
    "agent-payment-gateway",
    instructions=("AI Agent 支付审批网关。Agent 执行任何支付前必须调用 evaluate_payment；"
                  "ALLOW 才可付款，DENY 必须放弃，ASK 会返回 challenge——请向用户复述理由，"
                  "用户签名批准令牌后带 approval_token 重调。"
                  "策略只能由用户签名变更（set_policy/revoke_policy），Agent 只能用 "
                  "request_policy_change 提交申请。"),
)


# ---------------------------------------------------------------- Agent 日常

@server.tool(description=(
    "评估一笔支付意图。执行任何支付前必须调用。"
    "ALLOW 可付款；DENY 必须放弃；ASK 返回 challenge，需用户签名批准令牌后"
    "以 approval_token 重调。可选 mandate 参数传入用户签发的单次任务授权。"))
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
    "【Agent 专用】向用户申请调整策略/预算（如「日预算从 50 提高到 80」）。"
    "申请进入待批队列，用户批准后会签名发布新版本策略。Agent 无权直接改策略。"))
def request_policy_change(agent_id: str, request: str, reason: str) -> str:
    req = {"id": uuid.uuid4().hex[:10], "agent_id": agent_id, "request": request,
           "reason": reason, "ts": time.time(), "status": "pending"}
    policy_requests.append(req)
    return json.dumps({"submitted": True, "request_id": req["id"],
                       "note": "已提交用户待批队列"}, ensure_ascii=False)


@server.tool(description="查看当前生效的支付策略（legacy 兜底规则）")
def get_policy() -> str:
    legacy = gateway.legacy_policy
    if not legacy:
        return json.dumps({"note": "无 legacy 规则，仅签名策略生效"}, ensure_ascii=False)
    return json.dumps({"原始规则": legacy.raw_mandate,
                       **gateway.policy_summary}, ensure_ascii=False)


@server.tool(description="查看今日支出汇总")
def get_spend_report() -> str:
    today = date.today().isoformat()
    spent = gateway._spent.get(today, 0.0)
    budget = gateway.legacy_policy.daily_budget if gateway.legacy_policy else None
    return json.dumps({"date": today, "spent": round(spent, 2),
                       "daily_budget": budget,
                       "remaining": round(budget - spent, 2) if budget else None},
                      ensure_ascii=False)


@server.tool(description="查看最近的支付审批审计记录")
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
    "【用户专属】提交新版支付策略。必须携带用户钱包对策略文档的 EIP-191 签名；"
    "验签 + 版本链校验通过才接受。Agent 不应调用此工具。"))
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
    "【用户专属】撤销某条策略链（立即生效，默认拒绝后续支付）。"
    "必须携带用户钱包对撤销声明的签名。"))
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


@server.tool(description="查看某用户的策略版本历史（版本链 + 哈希，可与链上承诺比对）")
def get_policy_versions(owner: str) -> str:
    return json.dumps(gateway.store.history(owner), ensure_ascii=False, indent=2)


@server.tool(description="查看待批的 Agent 策略变更申请")
def list_policy_requests() -> str:
    return json.dumps(policy_requests, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 定制辅助

@server.tool(description="列出策略模板（个人助理/旅行/开发者 API/企业采购）")
def list_templates() -> str:
    import templates
    return json.dumps(templates.list_templates(), ensure_ascii=False, indent=2)


@server.tool(description="用模板 + 参数生成自然语言规则（再走编译+签名发布流程）")
def render_template(template_id: str, whitelist: list[str],
                    daily: float | None = None, per_tx: float | None = None,
                    ask_above: float | None = None) -> str:
    import templates
    overrides = {k: v for k, v in {"daily": daily, "per_tx": per_tx,
                                   "ask_above": ask_above}.items() if v is not None}
    try:
        rule = templates.render_template(template_id, whitelist, **overrides)
    except KeyError:
        return json.dumps({"error": f"未知模板：{template_id}"}, ensure_ascii=False)
    return json.dumps({"nl_rule": rule}, ensure_ascii=False)


def main() -> None:
    server.run()   # stdio 传输


if __name__ == "__main__":
    main()
