# -*- coding: utf-8 -*-
"""
setup_wallets.py — 生成 Base Sepolia 测试网钱包对（买家 + 商户）

⚠️ 这些私钥只用于测试网，没有任何真实价值。绝不要往这些地址转主网资产。
"""
import json
from pathlib import Path

from eth_account import Account

WALLETS_FILE = Path(__file__).parent / "wallets.testnet.json"


def main():
    if WALLETS_FILE.exists():
        w = json.loads(WALLETS_FILE.read_text())
        print("钱包已存在：")
    else:
        buyer, merchant = Account.create(), Account.create()
        w = {
            "_warning": "TESTNET ONLY - Base Sepolia. 无真实价值，勿存主网资产。",
            "network": "eip155:84532 (Base Sepolia)",
            "buyer": {"address": buyer.address, "private_key": buyer.key.hex()},
            "merchant": {"address": merchant.address, "private_key": merchant.key.hex()},
        }
        WALLETS_FILE.write_text(json.dumps(w, indent=2, ensure_ascii=False))
        print(f"已生成钱包对 → {WALLETS_FILE.name}\n")

    print(f"  买家地址（需要领测试 USDC）：{w['buyer']['address']}")
    print(f"  商户地址（收款方）：        {w['merchant']['address']}")
    print("""
下一步：给买家地址领取 Base Sepolia 测试 USDC（任选其一）：
  1. Coinbase CDP Faucet：https://portal.cdp.coinbase.com/products/faucet
     （选 Base Sepolia → USDC，粘贴买家地址）
  2. Circle Faucet：https://faucet.circle.com （选 Base Sepolia → USDC）

x402 的 exact 模式用 EIP-3009 签名转账，买家不需要 ETH 付 gas，
facilitator 会代付 gas 提交链上交易。
领水后运行：.venv/bin/python demo_real.py
""")


if __name__ == "__main__":
    main()
