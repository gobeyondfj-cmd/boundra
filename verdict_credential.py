# -*- coding: utf-8 -*-
"""
verdict_credential.py — 平台签名的评估凭证（boundra.verdict/v1）

每次 evaluate_payment 的 verdict 附带一份由平台密钥 EIP-191 签名的凭证，
回答"谁能证明这个 ALLOW/DENY/ASK 真的发生过、且是在这份策略下做出的"：

  - audit_id 链接到服务端审计日志条目
  - intent_hash 绑定具体支付意图（金额/收款方/用途，防张冠李戴）
  - policy_refs 绑定评估所用的策略版本哈希（可对照链上锚定）
  - decision/layer/reasons/spent_today 完整记录判定上下文

验证（任何人、离线）：
  1. 剥离 proof 字段，对凭证做 canonical JSON（sort_keys + 紧凑分隔符）
  2. EIP-191 recover 签名 → 应等于 issuer（平台签名地址，公开于
     https://api.boundra.io/verdict-key 与本模块 PLATFORM_ISSUER）

私钥只在服务端（Fly secret BOUNDRA_VERDICT_KEY），永不入库、永不打印。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from signed_policy import canonical

CREDENTIAL_TYPE = "boundra.verdict/v1"
# 平台签名地址 = 主网收入地址（同一密钥，凭证与收入流向同一身份）。
# 链上佐证：tx 0xf04d3497…eac7e5（Base 主网）中 0.003 USDC 的收款方。
PLATFORM_ISSUER = "0xF30955B6Be768370Da13fca237A5f252F6081d1C"


def intent_hash(intent: dict) -> str:
    """支付意图的绑定哈希（sha256 over canonical intent）。"""
    return hashlib.sha256(canonical({
        "amount": intent.get("amount"),
        "recipient": intent.get("recipient"),
        "purpose": intent.get("purpose"),
    }).encode()).hexdigest()


def build_credential_payload(verdict: dict, intent: dict, owner: str | None) -> dict:
    """从 verdict + 原始意图构造待签名凭证体（不含 proof）。"""
    return {
        "type": CREDENTIAL_TYPE,
        "issuer": PLATFORM_ISSUER,
        "audit_id": verdict.get("audit_id"),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owner": owner,
        "intent_hash": intent_hash(intent),
        "decision": verdict.get("decision"),
        "raw": verdict.get("raw"),
        "layer": verdict.get("layer"),
        "reasons": verdict.get("reasons") or [],
        "human": verdict.get("human"),
        "policy_refs": verdict.get("policy_refs") or [],
        "mandate_id": verdict.get("mandate_id"),
        "spent_today": verdict.get("spent_today"),
    }


@dataclass
class VerdictSigner:
    """平台凭证签名器。私钥仅存在于内存。"""
    private_key: str

    @property
    def address(self) -> str:
        from eth_account import Account
        return Account.from_key(self.private_key).address

    def sign(self, verdict: dict, intent: dict, owner: str | None) -> dict:
        """返回带 proof 的完整凭证。"""
        from eth_account import Account
        from eth_account.messages import encode_defunct
        payload = build_credential_payload(verdict, intent, owner)
        payload["issuer"] = self.address  # 以实际密钥地址为准
        sig = Account.sign_message(
            encode_defunct(text=canonical(payload)), self.private_key).signature.hex()
        return {**payload,
                "proof": {"type": "EIP-191", "signature": "0x" + sig.removeprefix("0x")}}


def verify_credential(credential: dict, expected_issuer: str = PLATFORM_ISSUER) -> tuple[bool, str]:
    """离线验证凭证。返回 (是否有效, 原因)。"""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    proof = credential.get("proof") or {}
    if proof.get("type") != "EIP-191" or not proof.get("signature"):
        return False, "missing or unsupported proof"
    if credential.get("type") != CREDENTIAL_TYPE:
        return False, f"unknown credential type: {credential.get('type')}"
    payload = {k: v for k, v in credential.items() if k != "proof"}
    try:
        sig = bytes.fromhex(proof["signature"].removeprefix("0x"))
        recovered = Account.recover_message(
            encode_defunct(text=canonical(payload)), signature=sig)
    except Exception as e:
        return False, f"signature recovery failed: {e}"
    if recovered.lower() != (payload.get("issuer") or "").lower():
        return False, f"signer {recovered} != declared issuer {payload.get('issuer')}"
    if recovered.lower() != expected_issuer.lower():
        return False, f"issuer {recovered} is not the Boundra platform key ({expected_issuer})"
    return True, "valid"
