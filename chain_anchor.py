# -*- coding: utf-8 -*-
"""
chain_anchor.py — 锚定后端：本地模拟 + Base Sepolia 真实链

LocalAnchor      ：JSONL 文件模拟（离线演示/测试用）
BaseSepoliaAnchor：真实合约读写（需 chain_config.json + 测试 ETH）

chain_config.json 格式：
{
  "rpc_url": "https://sepolia.base.org",
  "contract_address": "0x...",          # 部署后填入
  "writer_private_key": "0x..."         # 仅 anchor() 写入需要；只读核对不需要
}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("BOUNDRA_STATE_DIR") or (Path(__file__).parent / "state"))

ABI = [
    {"inputs": [{"name": "docHash", "type": "bytes32"},
                {"name": "version", "type": "uint256"}],
     "name": "anchor", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "version", "type": "uint256"},
                {"name": "docHash", "type": "bytes32"}],
     "name": "isAnchored", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}],
     "name": "latestVersion", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


class LocalAnchor:
    """本地锚定：模拟链上承诺日志（原型/测试）。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (STATE_DIR / "anchor_log.jsonl")
        self.path.parent.mkdir(exist_ok=True)

    def anchor(self, owner: str, version: int, hash_: str):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"owner": owner, "version": version, "hash": hash_,
                                "ts": time.time()}, ensure_ascii=False) + "\n")

    def is_anchored(self, owner: str, version: int, hash_: str) -> bool:
        if not self.path.exists():
            return False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if (r["owner"].lower() == owner.lower()
                    and r["version"] == version and r["hash"] == hash_):
                return True
        return False

    def latest_version(self, owner: str) -> int:
        v = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                if r["owner"].lower() == owner.lower():
                    v = max(v, r["version"])
        return v


class BaseSepoliaAnchor:
    """真实链锚定：Base Sepolia 上的 PolicyAnchor 合约。"""

    def __init__(self, config_path: Path | None = None):
        cfg = json.loads((config_path or Path(__file__).parent / "chain_config.json").read_text())
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))
        if not self.w3.is_connected():
            raise ConnectionError(f"无法连接 RPC：{cfg['rpc_url']}")
        self.contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(cfg["contract_address"]), abi=ABI)
        self._key = cfg.get("writer_private_key")

    @staticmethod
    def _b32(hash_hex: str) -> bytes:
        return bytes.fromhex(hash_hex.removeprefix("0x").rjust(64, "0"))

    def anchor(self, owner: str, version: int, hash_: str):
        if not self._key:
            raise PermissionError("chain_config.json 缺少 writer_private_key，无法写链")
        acct = self.w3.eth.account.from_key(self._key)
        if acct.address.lower() != owner.lower():
            raise PermissionError("合约要求 msg.sender == owner：写入密钥必须属于策略 owner")
        tx = self.contract.functions.anchor(self._b32(hash_), version).build_transaction({
            "from": acct.address,
            "nonce": self.w3.eth.get_transaction_count(acct.address),
            "gas": 120_000,
            "maxFeePerGas": self.w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(0.001, "gwei"),
            "chainId": 84532,
        })
        signed = acct.sign_transaction(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(signed.raw_transaction))
        if receipt.status != 1:
            raise RuntimeError(f"锚定交易失败：{receipt.transactionHash.hex()}")

    def is_anchored(self, owner: str, version: int, hash_: str) -> bool:
        return self.contract.functions.isAnchored(
            self.w3.to_checksum_address(owner), version, self._b32(hash_)).call()

    def latest_version(self, owner: str) -> int:
        return self.contract.functions.latestVersion(
            self.w3.to_checksum_address(owner)).call()


def load_anchor(prefer_chain: bool = True):
    """有 chain_config.json 就用真链，否则退回本地模拟。"""
    cfg = Path(__file__).parent / "chain_config.json"
    if prefer_chain and cfg.exists():
        try:
            return BaseSepoliaAnchor(cfg)
        except Exception as e:
            print(f"⚠️ 链上锚定不可用（{e}），退回本地模拟")
    return LocalAnchor()
