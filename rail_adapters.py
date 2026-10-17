# -*- coding: utf-8 -*-
"""
rail_adapters.py — 支付轨道适配层

Boundra 的定位是"支付前的决策点"：评估核心（gateway.py）对任何结算
轨道一视同仁。本层只做两件事：

  1. extract：把轨道原生的支付上下文（x402 的 402 上下文、未来的 AP2
     mandate、委托卡回调……）翻成统一的 RailContext → PaymentIntent
  2. abort / proceed：把网关裁决翻回该轨道的原生动作

x402 是第一个 adapter（拦截点：SDK v2 的 on_before_payment_creation
钩子，签名之前触发）。接入新轨道时评估核心零改动——这就是
CEO 日志 #002 的"多轨并存"对策在代码里的落点。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from policy import PaymentIntent


@dataclass
class RailContext:
    """从任意轨道提取的统一支付上下文"""
    amount: float
    recipient: str
    purpose: str
    expected_amount: Optional[float] = None
    source_content: str = ""
    rail: str = "unknown"
    raw: Any = None                     # 轨道原始对象，备查

    def to_intent(self) -> PaymentIntent:
        return PaymentIntent(amount=self.amount, recipient=self.recipient,
                             purpose=self.purpose,
                             expected_amount=self.expected_amount,
                             source_content=self.source_content)


class RailAdapter(ABC):
    """每个支付轨道实现一个适配器"""
    name: str = "abstract"

    @abstractmethod
    def extract(self, payment_ctx: Any, purpose: str,
                expected_amount: Optional[float] = None) -> RailContext:
        """轨道原生上下文 → 统一 RailContext"""

    @abstractmethod
    def abort(self, verdict: dict) -> Any:
        """DENY/ASK → 轨道原生的中止动作（必须让支付无法发生）"""

    def proceed(self, verdict: dict) -> Any:
        """ALLOW → 放行。大多数轨道返回 None 即继续。"""
        return None


class X402Adapter(RailAdapter):
    """x402 v2（exact 模式，USDC，EIP-3009 签名前拦截）"""

    name = "x402"
    USDC_DECIMALS = 6

    def extract(self, payment_ctx: Any, purpose: str,
                expected_amount: Optional[float] = None) -> RailContext:
        req = payment_ctx.selected_requirements
        return RailContext(
            amount=int(req.amount) / 10 ** self.USDC_DECIMALS,
            recipient=req.pay_to,
            purpose=purpose,
            expected_amount=expected_amount,
            source_content=self._description_of(payment_ctx),
            rail=self.name,
            raw=payment_ctx,
        )

    @staticmethod
    def _description_of(payment_ctx) -> str:
        pr = getattr(payment_ctx, "payment_required", None)
        if pr is None:
            return ""
        res = pr.get("resource") if isinstance(pr, dict) else getattr(pr, "resource", None)
        if res is None:
            return ""
        if isinstance(res, dict):
            return res.get("description", "") or ""
        return getattr(res, "description", "") or ""

    def abort(self, verdict: dict):
        from x402.schemas.hooks import AbortResult
        return AbortResult(
            reason=f"gateway_{verdict.get('layer') or 'policy'}",
            message="; ".join(verdict.get("reasons") or ["DENY"]))
