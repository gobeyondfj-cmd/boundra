# -*- coding: utf-8 -*-
"""
hosted_mcp.py — Boundra 托管 MCP endpoint（streamable-HTTP，多租户）

与本地 stdio 版（mcp_server.py）的区别：
  · 无 legacy 公共策略：没有签名策略的 owner 一律 default-deny
  · PolicyStore 落盘持久化（policies.json），跨重启/部署存活
  · 消费计数按 owner 命名空间（gateway._spent_key），单实例服务多用户
  · 无会话状态（stateless_http + json_response），适配 auto-stop 的托管环境

部署：被 paid_gateway_server.create_app() 挂载在 /mcp。
客户端配置（任一支持 streamable-http 的 MCP 客户端）：
  { "mcpServers": { "boundra": { "url": "https://api.boundra.io/mcp" } } }
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).parent))
from gateway import AUDIT_LOG, Gateway
from signed_policy import PolicyDocument, PolicyStore, Revocation

STATE_DIR = Path(os.environ.get("BOUNDRA_STATE_DIR") or (Path(__file__).parent / "state"))

# 托管实例：无 legacy 规则（无签名策略 = default-deny），策略链持久化
gateway = Gateway(None, store=PolicyStore(persist_path=STATE_DIR / "policies.json"))

# Agent 策略变更申请（内存队列；用户通过 list_policy_requests 拉取）
policy_requests: list[dict] = []

server = MCPServer(
    "boundra-hosted",
    instructions=("Boundra hosted payment approval gateway (multi-tenant). You MUST call "
                  "evaluate_payment with your owner's wallet address before any payment: "
                  "ALLOW = proceed, DENY = abandon, ASK = relay the reason to the user and "
                  "retry with approval_token after the user signs the challenge. Always pass "
                  "the same owner address on every call — spend limits and policies are keyed "
                  "to it. Policies change only via the owner's wallet signature "
                  "(set_policy/revoke_policy); agents may only file requests via "
                  "request_policy_change."),
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
                     mandate: dict | None = None) -> str:
    verdict = gateway.handle_tool_call("evaluate_payment", {
        "intent": {"amount": amount, "recipient": recipient, "purpose": purpose,
                   "expected_amount": expected_amount, "source_content": source_content},
        "approval_token": approval_token, "owner": owner,
        "agent_id": agent_id, "mandate": mandate,
        "human_approved": False,   # 托管版禁用 legacy 自证审批
    })
    return json.dumps(verdict, ensure_ascii=False)


@server.tool(description=(
    "[For agents] Ask the user for a policy/budget change (e.g. 'raise daily budget from 50 to 80'). "
    "The request is queued for user approval; the user signs and publishes a new policy version. "
    "Agents can NEVER change policy directly."))
def request_policy_change(agent_id: str, request: str, reason: str) -> str:
    import time
    import uuid
    req = {"id": uuid.uuid4().hex[:10], "agent_id": agent_id, "request": request,
           "reason": reason, "ts": time.time(), "status": "pending"}
    policy_requests.append(req)
    return json.dumps({"submitted": True, "request_id": req["id"],
                       "note": "queued for user approval"}, ensure_ascii=False)


@server.tool(description="View the active signed payment policy for an owner (and optionally an agent scope)")
def get_policy(owner: str, agent_id: str | None = None) -> str:
    out = {}
    for scope, aid in (("user", None), ("agent", agent_id)):
        if scope == "agent" and not aid:
            continue
        doc = gateway.store.active(owner, scope, aid)
        out[scope] = ({"version": doc.version, "hash": doc.hash,
                       "nl_rule": doc.nl_rule, "compiled": doc.compiled,
                       "revoked": False} if doc else
                      {"note": "no active policy" +
                               (" (revoked)" if gateway.store.is_revoked(owner, scope, aid) else "")})
    return json.dumps(out, ensure_ascii=False)


@server.tool(description="View today's spend summary for an owner")
def get_spend_report(owner: str) -> str:
    today = date.today().isoformat()
    spent = gateway.spent_today(owner)
    return json.dumps({"date": today, "owner": owner, "spent": round(spent, 2)},
                      ensure_ascii=False)


@server.tool(description="View recent payment-approval audit records for an owner")
def get_audit_trail(owner: str, limit: int = 10) -> str:
    if not AUDIT_LOG.exists():
        return "[]"
    records = [json.loads(x) for x in AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
    mine = [r for r in records if (r.get("owner") or "").lower() == owner.lower()]
    return json.dumps(mine[-limit:], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 用户专属（签名门控）

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


# ---------------------------------------------------------------- ASGI 入口

def build_hosted_mcp_app():
    """返回 (ASGI 应用, session_manager)。

    子应用挂载进 FastAPI 后收不到自己的 lifespan 事件，因此调用方必须在
    父应用的 lifespan 里运行 `async with session_manager.run(): yield`，
    否则请求会报 "Task group is not initialized"。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    # SDK 默认开启 DNS-rebinding 防护且默认白名单只含 localhost，
    # 线上必须把对外域名加进 allowed_hosts，否则 "Invalid Host header"
    public_hosts = [h.strip() for h in
                    os.environ.get("BOUNDRA_MCP_HOSTS",
                                   "api.boundra.io,boundra-api.fly.dev").split(",")
                    if h.strip()]
    allowed_hosts = public_hosts + [f"{h}:*" for h in public_hosts] + [
        "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*",
    ]
    security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_hosts,  # 非浏览器客户端一般不带 Origin；浏览器场景同源
    )
    sub_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,       # 无会话状态：适配 Fly auto-stop
        json_response=True,        # 纯 JSON 响应，不开 SSE 流
        max_sessions=200,          # 内存护栏（512MB 机器）
        session_idle_timeout=600,
        transport_security=security,
    )
    manager = getattr(server._lowlevel_server, "_session_manager", None)
    if manager is None:
        raise RuntimeError("MCP SDK 未暴露 session manager，无法接入父应用 lifespan")
    return sub_app, manager
