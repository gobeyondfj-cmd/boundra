# -*- coding: utf-8 -*-
"""
signed_policy.py — 用户签名的策略文档 + 哈希版本链 + 链上锚定接口

核心思想（对应"策略上链"讨论的落地形态）：
  · 策略是一份用户用钱包密钥签名的文档（EIP-191 personal_sign）
  · 版本链：每个新版本记录 prev_hash，篡改任何历史版本都会断链
  · 链上只存承诺（doc_hash + version + owner），不存全文 —— 隐私与完整性兼得
  · Agent / 网关对策略只有读权限：没有用户私钥就无法提交新版本

信任边界：谁持用户私钥，谁才能改策略。网关运营者、Agent、攻击者都不行。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from policy import Policy, compile_policy

STATE_DIR = Path(__file__).parent / "state"
ANCHOR_LOG = STATE_DIR / "anchor_log.jsonl"


# ---------------------------------------------------------------- 文档与哈希

def canonical(doc: dict) -> str:
    """确定性序列化（签名与哈希的共同输入）"""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def doc_hash(doc: dict) -> str:
    # 生产/上链统一用 keccak256（与合约 bytes32 一致）；
    # 离线环境（无 eth_utils）退回 sha256，仅供 demo.py 演示
    try:
        from eth_utils import keccak
        return keccak(text=canonical(doc)).hex()
    except ImportError:
        return hashlib.sha256(canonical(doc).encode()).hexdigest()


@dataclass
class PolicyDocument:
    owner: str            # 用户钱包地址（策略的信任根）
    scope: str            # "user"（全部 Agent）| "agent"（指定 Agent）
    agent_id: str | None  # scope=agent 时的 Agent 身份（钱包地址/凭证 ID）
    version: int
    nl_rule: str          # 自然语言规则原文（语义基准）
    compiled: dict        # 编译后的结构化策略（compile_policy 的产物）
    prev_hash: str        # 上一版本的 doc_hash；创世版本为 "0" * 64
    created_at: float = field(default_factory=time.time)
    signature: str = ""   # 用户对 canonical(本文档除签名字段) 的 EIP-191 签名
    hash: str = ""

    def unsigned_dict(self) -> dict:
        return {"owner": self.owner, "scope": self.scope, "agent_id": self.agent_id,
                "version": self.version, "nl_rule": self.nl_rule,
                "compiled": self.compiled, "prev_hash": self.prev_hash,
                "created_at": self.created_at}

    def finalize(self) -> "PolicyDocument":
        self.hash = doc_hash(self.unsigned_dict())
        return self

    def to_policy(self) -> Policy:
        p = Policy(raw_mandate=self.nl_rule)
        for k, v in self.compiled.items():
            if hasattr(p, k):
                setattr(p, k, v)
        return p


def new_document(owner: str, scope: str, agent_id: str | None,
                 version: int, nl_rule: str, prev_hash: str) -> PolicyDocument:
    compiled = compile_policy(nl_rule)
    doc = PolicyDocument(owner=owner, scope=scope, agent_id=agent_id,
                         version=version, nl_rule=nl_rule,
                         compiled={"per_tx_max": compiled.per_tx_max,
                                   "daily_budget": compiled.daily_budget,
                                   "allowed_recipients": compiled.allowed_recipients,
                                   "require_approval_above": compiled.require_approval_above},
                         prev_hash=prev_hash)
    return doc.finalize()


# ---------------------------------------------------------------- 签名与验签

def sign_document(doc: PolicyDocument, private_key: str) -> str:
    """用户侧操作：用钱包私钥对策略文档签名（EIP-191）"""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    msg = encode_defunct(text=canonical(doc.unsigned_dict()))
    return Account.sign_message(msg, private_key).signature.hex()


def verify_signature(doc: PolicyDocument) -> bool:
    """任何人可执行：恢复签名者地址，必须等于 doc.owner"""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        msg = encode_defunct(text=canonical(doc.unsigned_dict()))
        sig = bytes.fromhex(doc.signature.removeprefix("0x"))
        return Account.recover_message(msg, signature=sig).lower() == doc.owner.lower()
    except Exception:
        return False


# ---------------------------------------------------------------- 撤销

@dataclass
class Revocation:
    """用户签名的撤销声明：撤销某条策略链截至 revoked_through 的所有版本。

    撤销是链下签名声明（即时生效、零 gas）；可选地把其哈希也锚定上链。
    撤销后 active() 返回 None → 网关默认拒绝，直到用户签名发布新版本。
    """
    owner: str
    scope: str
    agent_id: str | None
    revoked_through: int          # 撤销到该版本号（含）为止
    created_at: float = field(default_factory=time.time)
    signature: str = ""

    def unsigned_dict(self) -> dict:
        return {"owner": self.owner, "scope": self.scope, "agent_id": self.agent_id,
                "revoked_through": self.revoked_through, "created_at": self.created_at}

    def sign(self, private_key: str) -> str:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        self.signature = Account.sign_message(
            encode_defunct(text=canonical(self.unsigned_dict())), private_key).signature.hex()
        return self.signature

    def verify(self) -> bool:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        try:
            sig = bytes.fromhex(self.signature.removeprefix("0x"))
            signer = Account.recover_message(
                encode_defunct(text=canonical(self.unsigned_dict())), signature=sig)
            return signer.lower() == self.owner.lower()
        except Exception:
            return False


# ---------------------------------------------------------------- 锚定（链上承诺）

# 统一从 chain_anchor 引入：本地模拟（LocalAnchor）或真实链（BaseSepoliaAnchor）
from chain_anchor import LocalAnchor       # noqa: E402


# ---------------------------------------------------------------- 策略存储

class PolicyStore:
    """按 owner + scope + agent_id 组织的版本化策略仓库。

    提交新版本的三道校验（缺一不可）：
      1. 签名有效且签名者 == owner（用户密钥是唯一的写入权限）
      2. 版本号 = 当前最新版本 + 1
      3. prev_hash == 当前最新版本的 hash（版本链不断）
    """

    def __init__(self, anchor: LocalAnchor | None = None):
        self.chains: dict[tuple, list[PolicyDocument]] = {}
        self.revocations: dict[tuple, int] = {}     # key -> revoked_through
        self.anchor = anchor or LocalAnchor()

    @staticmethod
    def _key(owner: str, scope: str, agent_id: str | None) -> tuple:
        return (owner.lower(), scope, (agent_id or "").lower())

    def submit(self, doc: PolicyDocument) -> PolicyDocument:
        if not doc.signature:
            raise PermissionError("policy is missing the user's signature — rejected")
        if not verify_signature(doc):
            raise PermissionError(
                f"signer is not the policy owner ({doc.owner}): policy changes must be signed by the owner's key")
        chain = self.chains.setdefault(self._key(doc.owner, doc.scope, doc.agent_id), [])
        expected_ver = (chain[-1].version + 1) if chain else 1
        expected_prev = chain[-1].hash if chain else doc.prev_hash
        if doc.version != expected_ver:
            raise ValueError(f"expected version {expected_ver}, got {doc.version}")
        if chain and doc.prev_hash != expected_prev:
            raise ValueError("prev_hash does not match the latest version on-chain: version chain broken")
        doc.finalize()
        chain.append(doc)
        self.anchor.anchor(doc.owner, doc.version, doc.hash)   # 链上承诺（模拟）
        return doc

    def active(self, owner: str, scope: str, agent_id: str | None = None) -> PolicyDocument | None:
        chain = self.chains.get(self._key(owner, scope, agent_id))
        if not chain:
            return None
        revoked = self.revocations.get(self._key(owner, scope, agent_id), 0)
        if revoked >= chain[-1].version:
            return None                     # 已撤销：视为无策略（默认拒绝）
        return chain[-1]

    def revoke(self, rec: "Revocation"):
        """撤销某条策略链（含已锚定的所有历史版本）。签名者必须 == owner。"""
        if not rec.signature:
            raise PermissionError("revocation request is missing the user's signature")
        if not rec.verify():
            raise PermissionError("revocation signer is not the policy owner")
        key = self._key(rec.owner, rec.scope, rec.agent_id)
        self.revocations[key] = max(rec.revoked_through,
                                    self.revocations.get(key, 0))

    def is_revoked(self, owner: str, scope: str, agent_id: str | None) -> bool:
        chain = self.chains.get(self._key(owner, scope, agent_id))
        if not chain:
            return False
        return self.revocations.get(self._key(owner, scope, agent_id), 0) >= chain[-1].version

    def check_integrity(self, doc: PolicyDocument) -> list[str]:
        """每次评估前的体检：签名仍有效 + 承诺已锚定 + 哈希未变"""
        problems = []
        if not verify_signature(doc):
            problems.append("signature verification failed")
        doc.finalize()
        if not self.anchor.is_anchored(doc.owner, doc.version, doc.hash):
            problems.append(f"hash of v{doc.version} not found in the anchor log (possible tampering)")
        return problems

    def history(self, owner: str) -> list[dict]:
        out = []
        for (o, scope, agent_id), chain in self.chains.items():
            if o == owner.lower():
                for d in chain:
                    out.append({"scope": scope, "agent_id": agent_id,
                                "version": d.version, "hash": d.hash,
                                "prev_hash": d.prev_hash, "nl_rule": d.nl_rule})
        return sorted(out, key=lambda x: (x["scope"], x["version"]))
