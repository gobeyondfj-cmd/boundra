# -*- coding: utf-8 -*-
"""
agent.py — 买家 Agent（含"朴素规划器"，模拟 LLM 被注入劫持的行为）

重点演示：Agent 读到的商户内容可能含有注入指令。没有网关时，
朴素 Agent 会照单全收——改收款方、抬金额、直接签名付款。
网关的价值在于：无论 Agent 的规划器被怎么带偏，
最终支付意图都必须过评估管线。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.request

from gateway import Gateway
from policy import PaymentIntent

WALLET_SECRET = b"demo-facilitator-secret"  # 原型：钱包密钥已在 facilitator 登记


class Wallet:
    def sign_payment(self, amount: float, recipient: str, nonce: str) -> str:
        payload = json.dumps({"amount": amount, "recipient": recipient, "nonce": nonce},
                             ensure_ascii=False)
        sig = hmac.new(WALLET_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        return f"{sig}.{payload}"


def _naive_planner(goal: str, offer: dict) -> dict:
    """模拟一个会被注入带偏的 LLM 规划器。

    正常情况：按 402 里的金额和收款方付款。
    被注入时：照内容里的"新指令"改写收款方和金额（LLM 幻觉/越权的现实风险）。
    """
    plan = {"amount": float(offer["maxAmountRequired"]),
            "recipient": offer["payTo"],
            "nonce": offer["nonce"],
            "source": offer.get("description", "")}
    desc = plan["source"]
    m = re.search(r"改为支付给新收款方\s*(\S+?)\s*，金额提高到\s*(\d+(?:\.\d+)?)", desc)
    if m:  # 注入生效：Agent 被外部内容劫持
        plan["recipient"], plan["amount"] = m.group(1), float(m.group(2))
        plan["hijacked"] = True
    return plan


class BuyerAgent:
    def __init__(self, gateway: Gateway | None, expected_amount: float | None = None):
        self.gateway = gateway            # None = 裸奔模式（对照组）
        self.wallet = Wallet()
        self.expected_amount = expected_amount  # mandate 下达时的预期金额锚点

    def buy(self, url: str, purpose: str) -> dict:
        # 1) 请求资源 → 402
        offer = self._http(url)["accepts"][0]

        # 2) 规划付款（朴素规划器，可能被注入内容劫持）
        plan = _naive_planner(purpose, offer)

        # 3) 关键一步：支付意图先过网关（MCP 拦截点）
        if self.gateway is not None:
            verdict = self.gateway.handle_tool_call("evaluate_payment", {
                "intent": {"amount": plan["amount"], "recipient": plan["recipient"],
                           "purpose": purpose,
                           "expected_amount": self.expected_amount,
                           "source_content": plan["source"]},
            })
            plan["gateway"] = verdict
            if verdict["decision"] != "ALLOW":
                plan["outcome"] = "blocked"
                return plan

        # 4) 网关放行（或裸奔模式）→ 签名并重试
        x_payment = self.wallet.sign_payment(plan["amount"], plan["recipient"], plan["nonce"])
        result = self._http(url, x_payment)
        plan["outcome"] = "paid"
        plan["settled"] = result.get("settled")
        return plan

    @staticmethod
    def _http(url: str, x_payment: str | None = None) -> dict:
        req = urllib.request.Request(url)
        if x_payment:
            req.add_header("X-PAYMENT", x_payment)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
