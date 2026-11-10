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
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

sys.path.insert(0, str(Path(__file__).parent))
from gateway import AUDIT_LOG, Gateway
from signed_policy import PolicyDocument, PolicyStore, Revocation
from spend_ledger import SpendLedger

STATE_DIR = Path(os.environ.get("BOUNDRA_STATE_DIR") or (Path(__file__).parent / "state"))

# 平台凭证签名器：配置了 BOUNDRA_VERDICT_KEY（私钥，仅存在于部署密管）时，
# 每次 evaluate_payment 的 verdict 附带可离线验证的 EIP-191 签名凭证
_verdict_key = os.environ.get("BOUNDRA_VERDICT_KEY")
_verdict_signer = None
if _verdict_key:
    from verdict_credential import VerdictSigner
    _verdict_signer = VerdictSigner(_verdict_key)

# 托管实例：无 legacy 规则（无签名策略 = default-deny），策略链持久化
gateway = Gateway(None, store=PolicyStore(persist_path=STATE_DIR / "policies.json"),
                  verdict_signer=_verdict_signer,
                  ledger=SpendLedger(STATE_DIR / "spend.db"))

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


OwnerArg = Annotated[str, Field(description="Owner wallet address that scopes policy, budget and audit state")]
OptionalOwnerArg = Annotated[str | None, Field(description="Owner wallet address; required for a persistent hosted budget reservation")]
AgentArg = Annotated[str, Field(description="Stable identifier of the agent requesting this operation")]
OptionalAgentArg = Annotated[str | None, Field(description="Optional agent identifier for agent-scoped policy or attribution")]
EvaluationArg = Annotated[str, Field(description="Evaluation identifier returned by an ALLOW verdict")]
ReservationTokenArg = Annotated[str, Field(description="Private reservation capability returned with the evaluation; never log or publish it")]

READ_TOOL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
IDEMPOTENT_WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
REVOCATION_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


# ---------------------------------------------------------------- Agent 日常

@server.tool(description=(
    "Evaluate a payment intent. MUST be called before executing any payment. "
    "ALLOW = you may pay; DENY = you must abandon; ASK = a challenge is returned — "
    "relay the reason to the user, then retry with approval_token once the user signs. "
    "An optional mandate parameter carries a user-issued single-task authorization."),
    annotations=WRITE_TOOL)
def evaluate_payment(
        amount: Annotated[float, Field(description="Payment amount in USDC or the policy's accounting unit")],
        recipient: Annotated[str, Field(description="Payee address, host or recipient identifier to evaluate")],
        purpose: Annotated[str, Field(description="Human-readable reason for this purchase")],
        expected_amount: Annotated[float | None, Field(description="Caller-accepted price; deterministic checks reject a different declared amount")] = None,
        expected_recipient: Annotated[str | None, Field(description="Caller-approved payee; deterministic checks reject substitution")] = None,
        resource: Annotated[str | None, Field(description="Resource URL or host/path used for per-endpoint budgets")] = None,
        method: Annotated[str | None, Field(description="HTTP method of the paid resource, such as GET or POST")] = None,
        network: Annotated[str | None, Field(description="CAIP-2 payment network, for example eip155:8453")] = None,
        asset: Annotated[str | None, Field(description="Payment asset identifier or contract address")] = None,
        source_content: Annotated[str, Field(description="Merchant content that triggered payment; semantic evaluation scans it for injection and drift")] = "",
        approval_token: Annotated[str | None, Field(description="Wallet-signed approval token for the exact ASK challenge and payment intent")] = None,
        owner: OptionalOwnerArg = None,
        agent_id: OptionalAgentArg = None,
        mandate: Annotated[dict | None, Field(description="Optional user-issued single-task payment mandate object")] = None,
        purchase_id: Annotated[str | None, Field(description="Caller-generated ID for one logical purchase; concurrent duplicates cannot obtain a second ALLOW")] = None) -> str:
    verdict = gateway.handle_tool_call("evaluate_payment", {
        "intent": {"amount": amount, "recipient": recipient, "purpose": purpose,
                   "expected_amount": expected_amount,
                   "expected_recipient": expected_recipient,
                   "resource": resource, "method": method,
                   "network": network, "asset": asset,
                   "source_content": source_content},
        "approval_token": approval_token, "owner": owner,
        "agent_id": agent_id, "mandate": mandate,
        "purchase_id": purchase_id,
        "human_approved": False,   # 托管版禁用 legacy 自证审批
    })
    return json.dumps(verdict, ensure_ascii=False)


@server.tool(description=(
    "Confirm that an ALLOWed payment settled. Requires the evaluation_id and the "
    "reservation_token returned by evaluate_payment, plus the real transaction/hash reference. "
    "Idempotent for the same evaluation; never call this before settlement."),
    annotations=IDEMPOTENT_WRITE_TOOL)
def settle_evaluation(
        evaluation_id: EvaluationArg,
        reservation_token: ReservationTokenArg,
        settlement_ref: Annotated[str, Field(description="Real transaction hash or facilitator settlement reference")]) -> str:
    try:
        result = gateway.settle_evaluation(evaluation_id, reservation_token, settlement_ref)
    except (PermissionError, RuntimeError, ValueError) as e:
        result = {"settled": False, "error": str(e)}
    return json.dumps(result, ensure_ascii=False)


@server.tool(description=(
    "Release a reserved ALLOW budget when the payment failed or was abandoned. "
    "Requires the evaluation_id and reservation_token returned by evaluate_payment."),
    annotations=IDEMPOTENT_WRITE_TOOL)
def release_evaluation(
        evaluation_id: EvaluationArg,
        reservation_token: ReservationTokenArg) -> str:
    try:
        result = gateway.release_evaluation(evaluation_id, reservation_token)
    except (PermissionError, RuntimeError, ValueError) as e:
        result = {"released": False, "error": str(e)}
    return json.dumps(result, ensure_ascii=False)


@server.tool(description="Inspect the reservation/settlement state of an evaluation",
             annotations=READ_TOOL)
def get_evaluation(evaluation_id: EvaluationArg) -> str:
    result = gateway.ledger.get(evaluation_id) if gateway.ledger else None
    return json.dumps(result or {"error": "evaluation not found"}, ensure_ascii=False)


@server.tool(description=(
    "[For agents] Ask the user for a policy/budget change (e.g. 'raise daily budget from 50 to 80'). "
    "The request is queued for user approval; the user signs and publishes a new policy version. "
    "Agents can NEVER change policy directly."), annotations=WRITE_TOOL)
def request_policy_change(
        agent_id: AgentArg,
        request: Annotated[str, Field(description="Requested policy or budget change in natural language")],
        reason: Annotated[str, Field(description="Evidence or explanation for why the change is needed")]) -> str:
    import time
    import uuid
    req = {"id": uuid.uuid4().hex[:10], "agent_id": agent_id, "request": request,
           "reason": reason, "ts": time.time(), "status": "pending"}
    policy_requests.append(req)
    return json.dumps({"submitted": True, "request_id": req["id"],
                       "note": "queued for user approval"}, ensure_ascii=False)


@server.tool(description="View the active signed payment policy for an owner (and optionally an agent scope)",
             annotations=READ_TOOL)
def get_policy(owner: OwnerArg, agent_id: OptionalAgentArg = None) -> str:
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


@server.tool(description="View today's spend summary for an owner", annotations=READ_TOOL)
def get_spend_report(owner: OwnerArg) -> str:
    today = date.today().isoformat()
    spent = gateway.spent_today(owner)
    return json.dumps({"date": today, "owner": owner, "spent": round(spent, 2)},
                      ensure_ascii=False)


@server.tool(description="View recent payment-approval audit records for an owner",
             annotations=READ_TOOL)
def get_audit_trail(
        owner: OwnerArg,
        limit: Annotated[int, Field(description="Maximum number of most recent audit records to return")] = 10) -> str:
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
            "require_approval_above": c.require_approval_above,
            "expected_amount_tolerance": c.expected_amount_tolerance,
            "per_resource_daily_budget": c.per_resource_daily_budget}


@server.tool(description=(
    "[USER ONLY] Submit a new policy version. Must carry the user's wallet EIP-191 signature "
    "over the policy document; accepted only after signature + version-chain checks pass. "
    "Agents MUST NOT call this tool."), annotations=WRITE_TOOL)
def set_policy(
        owner: OwnerArg,
        scope: Annotated[str, Field(description="Policy scope: user for the owner default or agent for one agent")],
        version: Annotated[int, Field(description="Monotonically increasing policy version number")],
        nl_rule: Annotated[str, Field(description="Natural-language spending rule to compile and enforce")],
        prev_hash: Annotated[str, Field(description="Previous policy hash, or 64 zero characters for a genesis policy")],
        created_at: Annotated[float, Field(description="Policy creation time as a Unix timestamp in seconds")],
        signature: Annotated[str, Field(description="Owner wallet EIP-191 signature over the policy document")],
        agent_id: OptionalAgentArg = None) -> str:
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
    "default to DENY). Must carry the user's wallet signature over the revocation statement."),
    annotations=REVOCATION_TOOL)
def revoke_policy(
        owner: OwnerArg,
        scope: Annotated[str, Field(description="Policy scope to revoke: user or agent")],
        revoked_through: Annotated[int, Field(description="Highest policy version covered by this revocation")],
        created_at: Annotated[float, Field(description="Revocation creation time as a Unix timestamp in seconds")],
        signature: Annotated[str, Field(description="Owner wallet signature over the revocation statement")],
        agent_id: OptionalAgentArg = None) -> str:
    rec = Revocation(owner=owner, scope=scope, agent_id=agent_id,
                     revoked_through=revoked_through, created_at=created_at,
                     signature=signature)
    try:
        result = gateway.revoke_policy(rec)
    except PermissionError as e:
        return json.dumps({"revoked": False, "error": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@server.tool(description="View a user's policy version history (version chain + hashes, verifiable against on-chain commitments)",
             annotations=READ_TOOL)
def get_policy_versions(owner: OwnerArg) -> str:
    return json.dumps(gateway.store.history(owner), ensure_ascii=False, indent=2)


@server.tool(description="List pending agent policy-change requests", annotations=READ_TOOL)
def list_policy_requests() -> str:
    return json.dumps(policy_requests, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 定制辅助

@server.tool(description="List policy templates (personal assistant / travel / dev API / procurement)",
             annotations=READ_TOOL)
def list_templates() -> str:
    import templates
    return json.dumps(templates.list_templates(), ensure_ascii=False, indent=2)


@server.tool(description="Render a natural-language rule from a template + parameters (then compile, sign & publish)",
             annotations=READ_TOOL)
def render_template(
        template_id: Annotated[str, Field(description="Identifier returned by list_templates")],
        whitelist: Annotated[list[str], Field(description="Approved recipient hosts, addresses or identifiers")],
        daily: Annotated[float | None, Field(description="Optional total daily spending cap override")] = None,
        per_tx: Annotated[float | None, Field(description="Optional per-transaction spending cap override")] = None,
        ask_above: Annotated[float | None, Field(description="Optional amount above which human approval is required")] = None) -> str:
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
