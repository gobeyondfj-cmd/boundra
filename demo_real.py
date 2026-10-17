# -*- coding: utf-8 -*-
"""
demo_real.py — 真实 x402 + Base Sepolia 测试网端到端演示

  场景 A：合规购买 → 网关放行 → 真实链上结算（Base Sepolia USDC）
  场景 B：商户元数据含注入指令 → 网关语义层在签名前拦截 → 无链上交易

前置：先运行 .venv/bin/python setup_wallets.py 并按指引领测试 USDC。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BASE = "http://127.0.0.1:8402"


def hr(t):
    print(f"\n{'─' * 62}\n{t}\n{'─' * 62}")


def ensure_wallets() -> dict | None:
    f = HERE / "wallets.testnet.json"
    if not f.exists():
        subprocess.run([sys.executable, "setup_wallets.py"], cwd=HERE)
        return None
    return json.loads(f.read_text())


def start_server() -> bool:
    import uvicorn
    from x402_resource_server import create_app
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=8402, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):                          # 等服务就绪（未付费应得 402）
        try:
            urllib.request.urlopen(f"{BASE}/api/weather", timeout=2)
        except urllib.error.HTTPError as e:
            if e.code == 402:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


async def main():
    hr("第 0 步：检查测试网钱包")
    wallets = ensure_wallets()
    if wallets is None:
        print("⚠️ 钱包刚生成。请先按上面的指引领取 Base Sepolia 测试 USDC，"
              "然后重新运行本脚本。")
        return
    merchant = wallets["merchant"]["address"]
    print(f"商户收款地址：{merchant}")

    # 把商户地址写进自然语言规则的白名单（MCP server 启动时读取）
    (HERE / "policy_rule.txt").write_text(
        f"每天 API 开销不超过 50 美元，单笔不超过 10 美元；"
        f"只允许付给白名单服务商（{merchant}）；"
        f"超过 5 美元要我手机确认", encoding="utf-8")
    print(f"策略规则：单笔≤$10、日预算$50、白名单={{商户地址}}、>$5 转人工")

    hr("第 1 步：启动真实 x402 收款服务端（FastAPI + x402 中间件）")
    if not start_server():
        print("❌ 服务端未能就绪")
        return
    print(f"✔ 服务端就绪：未付费请求返回 HTTP 402（facilitator: https://x402.org/facilitator）")

    from x402_buyer import buy

    hr("场景 A · 合规购买 $0.01 天气数据（期望：放行 → 真实链上结算）")
    a = await buy(f"{BASE}/api/weather", purpose="查上海天气", expected_amount=0.01)
    if a["status"] == 200 and a.get("tx"):
        print(f"   ✅ 支付成功！数据：{a['data']}")
        print(f"   ⛓  链上交易：https://sepolia.basescan.org/tx/{a['tx']}")
    else:
        print(f"   ⚠️ 未能完成结算：status={a['status']} {a.get('error') or a.get('body', '')}")
        print("   最常见原因：买家地址没有 Base Sepolia USDC。"
              "请运行 .venv/bin/python setup_wallets.py 查看领水指引。")

    hr("场景 B · 恶意商户元数据夹带注入指令（期望：签名前被网关拦下）")
    b = await buy(f"{BASE}/api/weather-evil", purpose="查天气", expected_amount=0.01)
    g = b.get("gateway") or {}
    if g.get("decision") == "DENY":
        print(f"   ⛔ 已拦截：{g['reasons'][0]}")
        print("   EIP-3009 签名从未发生，链上零交易。")
    else:
        print(f"   ❌ 意外结果：{json.dumps(b, ensure_ascii=False, default=str)[:300]}")

    hr("完成")
    print("审计轨迹见 state/audit.jsonl；把 mcp_server.py 挂进 Claude Desktop 的")
    print("配置方法见 README.md。")


if __name__ == "__main__":
    asyncio.run(main())
