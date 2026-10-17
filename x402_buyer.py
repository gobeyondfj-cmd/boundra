# -*- coding: utf-8 -*-
"""
x402_buyer.py — 真实 x402 买家客户端，支付签名前经过 MCP 审批网关

关键机制：x402 SDK v2 原生的 on_before_payment_creation 钩子。
Agent 收到 402 后、用钱包做 EIP-3009 签名前，SDK 先回调我们的 hook；
hook 通过 MCP 协议（stdio）问网关"该不该付"，DENY 则返回 AbortResult
中止支付——签名根本不会发生。这正是"策略决策点"在真实协议栈里的位置。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

from eth_account import Account
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from x402 import x402Client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

from rail_adapters import X402Adapter

HERE = Path(__file__).parent
NETWORK = "eip155:84532"


async def buy(url: str, purpose: str, expected_amount: float,
              use_gateway: bool = True, human_approved: bool = False) -> dict:
    wallets = json.loads((HERE / "wallets.testnet.json").read_text())
    account = Account.from_key(wallets["buyer"]["private_key"])
    print(f"   买家钱包：{account.address}")

    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account), networks=NETWORK)

    outcome: dict = {"gateway": None, "status": None, "tx": None}

    async def run(http_session_mcp: ClientSession | None):
        adapter = X402Adapter()   # 轨道适配层：extract/abort，评估核心与轨道解耦

        # —— 核心钩子：签名之前先过网关 ——
        async def gateway_hook(ctx):
            rail_ctx = adapter.extract(ctx, purpose=purpose,
                                       expected_amount=expected_amount)
            print(f"   [hook] 收到 402：{rail_ctx.amount} USDC → {rail_ctx.recipient}")
            if http_session_mcp is None:
                return None                      # 裸奔模式：直接放行去签名
            r = await http_session_mcp.call_tool("evaluate_payment", {
                "amount": rail_ctx.amount, "recipient": rail_ctx.recipient,
                "purpose": purpose, "expected_amount": expected_amount,
                "source_content": rail_ctx.source_content,
                "human_approved": human_approved,
            })
            payload = r.content[0].text
            verdict = payload if isinstance(payload, dict) else json.loads(payload)
            outcome["gateway"] = verdict
            icon = {"ALLOW": "✅", "DENY": "⛔", "ASK": "🔔"}.get(verdict["decision"], "?")
            print(f"   [hook] 网关裁决：{icon} {verdict['decision']}"
                  f"（layer={verdict['layer']}）{' — ' + verdict['reasons'][0] if verdict['reasons'] else ''}")
            if verdict["decision"] == "ALLOW":
                return adapter.proceed(verdict)  # 放行，SDK 继续签名
            return adapter.abort(verdict)        # AbortResult：签名不会发生

        client.on_before_payment_creation(gateway_hook)

        async with wrapHttpxWithPayment(client) as http:
            try:
                resp = await http.get(url, timeout=60)
            except Exception as e:               # AbortResult / 验签失败等
                outcome["status"] = "aborted" if outcome["gateway"] else "error"
                outcome["error"] = f"{type(e).__name__}: {e}"
                return outcome

        outcome["status"] = resp.status_code
        pr = resp.headers.get("PAYMENT-RESPONSE") or resp.headers.get("X-PAYMENT-RESPONSE")
        if pr:
            try:
                info = json.loads(base64.b64decode(pr))
                outcome["tx"] = info.get("transaction")
            except Exception:
                pass
        if resp.status_code == 200:
            outcome["data"] = resp.json()
        else:
            outcome["body"] = resp.text[:300]
        return outcome

    if use_gateway:
        params = StdioServerParameters(command=sys.executable,
                                       args=["mcp_server.py"], cwd=str(HERE))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await run(session)
    return await run(None)


if __name__ == "__main__":
    result = asyncio.run(buy("http://127.0.0.1:8402/api/weather",
                             "查上海天气", expected_amount=0.01))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
