# -*- coding: utf-8 -*-
"""
approval.py — 签名批准令牌：取代 Agent 自证的 human_approved

设计：网关裁决 ASK 时生成 challenge（绑定具体支付意图 + 策略版本 + 时效）；
用户在自己的设备上用钱包私钥对 challenge 签名 → 批准令牌；
Agent 持令牌重调 evaluate_payment，网关验签 + 逐字段比对意图后才放行。

令牌绑定支付意图的全部字段——用户对"$6 付给 A"的批准，不能被挪用到
"$600 付给 B"。这就是"人对具体交易的授权证明"（AP2 Mandate 的最小形态）。
"""
from __future__ import annotations

import base64
import json
import time
import uuid

APPROVAL_TTL_SECONDS = 300   # 令牌 5 分钟有效


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_challenge(intent: dict, policy_refs: list) -> dict:
    """网关侧：ASK 时生成，随裁决一起返回给 Agent 转交用户"""
    return {"intent": {"amount": intent["amount"], "recipient": intent["recipient"],
                       "purpose": intent["purpose"]},
            "policy_versions": [f"{r['scope']}:v{r['version']}" for r in policy_refs],
            "nonce": uuid.uuid4().hex[:12],
            "exp": time.time() + APPROVAL_TTL_SECONDS}


def sign_approval(challenge: dict, private_key: str) -> str:
    """用户侧（手机/硬件密钥）：对 challenge 签名，产出批准令牌"""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    sig = Account.sign_message(encode_defunct(text=_canonical(challenge)),
                               private_key).signature.hex()
    return base64.urlsafe_b64encode(
        json.dumps({"challenge": challenge, "sig": sig}).encode()).decode()


def verify_approval(token: str, intent: dict, owner: str,
                    challenges_seen: set) -> tuple[bool, str]:
    """网关侧验令牌：签名者==owner、未过期、意图逐字段一致、nonce 未重用"""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        payload = json.loads(base64.urlsafe_b64decode(token))
        ch, sig = payload["challenge"], bytes.fromhex(payload["sig"].removeprefix("0x"))
        signer = Account.recover_message(
            encode_defunct(text=_canonical(ch)), signature=sig)
    except Exception:
        return False, "令牌格式非法"
    if signer.lower() != owner.lower():
        return False, f"签名者 {signer} 不是策略 owner"
    if time.time() > ch.get("exp", 0):
        return False, "令牌已过期"
    if ch["nonce"] in challenges_seen:
        return False, "令牌已被使用（防重放）"
    want = ch.get("intent", {})
    got = {"amount": intent["amount"], "recipient": intent["recipient"],
           "purpose": intent["purpose"]}
    if want != got:
        return False, f"令牌绑定的意图（{want}）与本次提交（{got}）不一致"
    challenges_seen.add(ch["nonce"])
    return True, "批准令牌验签通过"
