# -*- coding: utf-8 -*-
"""
mandate.py — 任务 Mandate：策略第 4 层，单次授权，用完即焚

用户对"一次具体任务"的签名授权（AP2 Intent Mandate 的最小形态）：
  "帮我去 airline.example 订这张机票，预算 $400 以内，1 小时内有效"

与长期策略的区别：策略是持续规则（版本链演进），Mandate 是单次令牌
（nonce 防重放、过期作废、金额封顶、收款方可选限定）。
评估时 Mandate 与长期策略同时生效，取最严。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Mandate:
    owner: str                       # 用户钱包地址
    mandate_id: str
    purpose: str                     # 任务描述（语义锚点）
    max_amount: float                # 金额封顶
    allowed_recipients: list = field(default_factory=list)  # 空 = 不限（仍受长期策略约束）
    expires_at: float = 0            # 过期时间戳
    signature: str = ""

    def unsigned_dict(self) -> dict:
        return {"owner": self.owner, "mandate_id": self.mandate_id,
                "purpose": self.purpose, "max_amount": self.max_amount,
                "allowed_recipients": self.allowed_recipients,
                "expires_at": self.expires_at}


def new_mandate(owner: str, purpose: str, max_amount: float,
                allowed_recipients: list | None = None,
                ttl_seconds: float = 3600) -> Mandate:
    return Mandate(owner=owner, mandate_id=uuid.uuid4().hex[:12], purpose=purpose,
                   max_amount=max_amount, allowed_recipients=allowed_recipients or [],
                   expires_at=time.time() + ttl_seconds)


def sign_mandate(m: Mandate, private_key: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    return Account.sign_message(encode_defunct(text=_canonical(m.unsigned_dict())),
                                private_key).signature.hex()


def verify_mandate(m: Mandate) -> tuple[bool, str]:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        sig = bytes.fromhex(m.signature.removeprefix("0x"))
        signer = Account.recover_message(
            encode_defunct(text=_canonical(m.unsigned_dict())), signature=sig)
    except Exception:
        return False, "Mandate 签名非法"
    if signer.lower() != m.owner.lower():
        return False, "Mandate 签名者不是 owner"
    if time.time() > m.expires_at:
        return False, "Mandate 已过期"
    return True, "ok"


def check_against_intent(m: Mandate, intent: dict) -> tuple[bool, str]:
    """Mandate 层评估：金额封顶 + 收款方限定（语义匹配由语义层负责）"""
    if intent["amount"] > m.max_amount:
        return False, f"金额 ${intent['amount']} 超过 Mandate 封顶 ${m.max_amount}"
    if m.allowed_recipients and intent["recipient"] not in m.allowed_recipients:
        return False, f"收款方 {intent['recipient']} 不在 Mandate 限定范围 {m.allowed_recipients}"
    return True, "ok"
