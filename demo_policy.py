# -*- coding: utf-8 -*-
"""
demo_policy.py — 用户签名策略 + 哈希版本链 + 锚定 的端到端演示

跑法：.venv/bin/python demo_policy.py  （需先运行过 setup_wallets.py）

演示七个场景：
  ① 用户用钱包签名 user 级策略 v1        → 网关验签接受
  ② 用户再给"旅行 Agent"签发更严的策略    → 接受；两层叠加取最严
  ③ 合规/超限支付                        → 叠加裁决生效，审计带版本锚点
  ④ 攻击者冒名提交"无限额"策略            → 验签失败，拒绝
  ⑤ Agent 试图无签名改策略               → 拒绝
  ⑥ 用户签名 v2 收紧规则                  → 版本链接续，v1 即刻失效
  ⑦ 篡改历史版本文档                      → 签名失效 + 断链，拒绝
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_account import Account
from gateway import Gateway
from signed_policy import ANCHOR_LOG, LocalAnchor, PolicyStore, new_document, sign_document

HERE = Path(__file__).parent
GENESIS = "0" * 64
TRAVEL_AGENT = "travel-agent-0x7a9f"


def hr(t):
    print(f"\n{'─' * 62}\n{t}\n{'─' * 62}")


def show(tag, ok, detail):
    print(f"   {'✔' if ok else '✗'} {tag}：{detail}")


def main():
    wallets = json.loads((HERE / "wallets.testnet.json").read_text())
    owner_addr, owner_key = wallets["buyer"]["address"], wallets["buyer"]["private_key"]
    attacker = Account.create()

    # 干净的演示环境：独立锚定日志与 store
    anchor = LocalAnchor(HERE / "state" / "anchor_log_demo.jsonl")
    anchor.path.unlink(missing_ok=True)
    gw = Gateway(nl_rule=None, store=PolicyStore(anchor))

    hr("① 用户 A 签名 user 级策略 v1（所有 Agent 的总规则）")
    user_v1_rule = ("每天所有 Agent 开销不超过 100 美元，单笔不超过 20 美元；"
                    "只允许付给白名单服务商（airline.example, hotel.example）；"
                    "超过 15 美元要我手机确认")
    doc = new_document(owner_addr, "user", None, 1, user_v1_rule, GENESIS)
    doc.signature = sign_document(doc, owner_key)
    r = gw.set_policy(doc)
    show("提交", r["accepted"], f"v{r['version']} 已接受，hash={r['hash'][:16]}…（已锚定）")

    hr("② 用户 A 再给旅行 Agent 签发更严的 agent 级策略 v1")
    agent_v1_rule = ("每天开销不超过 60 美元，单笔不超过 8 美元；"
                     "只允许付给白名单服务商（airline.example, hotel.example）；"
                     "超过 5 美元要我手机确认")
    doc = new_document(owner_addr, "agent", TRAVEL_AGENT, 1, agent_v1_rule, GENESIS)
    doc.signature = sign_document(doc, owner_key)
    r = gw.set_policy(doc)
    show("提交", r["accepted"], f"agent 级 v{r['version']}，hash={r['hash'][:16]}…")

    hr("③ 叠加裁决：同一笔支付要同时过两层策略（取最严）")
    v = gw.handle_tool_call("evaluate_payment", {
        "intent": {"amount": 10.0, "recipient": "airline.example",
                   "purpose": "订机票", "expected_amount": 10.0},
        "owner": owner_addr, "agent_id": TRAVEL_AGENT})
    show("$10 订机票", v["decision"] == "DENY" and v["layer"] == "limit",
         f"{v['decision']} — user 层允许(≤$20)，agent 层拦截(≤$8)：{v['reasons'][0]}")
    print(f"      审计锚点：{json.dumps(v['policy_refs'], ensure_ascii=False)}")

    gw.human_approval_handler = lambda i, d: True   # 模拟人批准
    v = gw.handle_tool_call("evaluate_payment", {
        "intent": {"amount": 6.0, "recipient": "airline.example",
                   "purpose": "改签费", "expected_amount": 6.0},
        "owner": owner_addr, "agent_id": TRAVEL_AGENT})
    show("$6 改签费（agent 层 >$5 转人工，人批准）",
         v["decision"] == "ALLOW" and v["raw"] == "ASK",
         f"raw={v['raw']} → 人工回执={v['human']} → {v['decision']}")

    hr("④ 攻击者冒名：用攻击者密钥签名一份「无限额」策略，冒称 owner=A")
    evil = new_document(owner_addr, "user", None, 2,
                        "每天开销不超过 999999 美元，单笔不超过 999999 美元",
                        gw.store.active(owner_addr, "user").hash)
    evil.signature = sign_document(evil, attacker.key.hex())   # 攻击者自己的钥匙
    try:
        gw.set_policy(evil)
        show("冒名策略", False, "被接受了！")
    except PermissionError as e:
        show("冒名策略", True, f"拒绝 — {e}")

    hr("⑤ Agent 试图无签名提交策略变更")
    doc = new_document(owner_addr, "agent", TRAVEL_AGENT, 2,
                       "每天开销不超过 99999 美元，单笔不超过 99999 美元", "x")
    try:
        gw.set_policy(doc)
        show("无签名策略", False, "被接受了！")
    except PermissionError as e:
        show("无签名策略", True, f"拒绝 — {e}")

    hr("⑥ 用户 A 签名 v2：收紧 user 层（单笔 $20 → $5）")
    prev = gw.store.active(owner_addr, "user")
    doc = new_document(owner_addr, "user", None, 2,
                       "每天所有 Agent 开销不超过 100 美元，单笔不超过 5 美元；"
                       "只允许付给白名单服务商（airline.example, hotel.example）；"
                       "超过 3 美元要我手机确认", prev.hash)
    doc.signature = sign_document(doc, owner_key)
    r = gw.set_policy(doc)
    show("提交 v2", r["accepted"],
         f"prev_hash 接续 v1（…{prev.hash[-8:]} → …{r['hash'][-8:]}）")
    v = gw.handle_tool_call("evaluate_payment", {
        "intent": {"amount": 10.0, "recipient": "airline.example",
                   "purpose": "订机票", "expected_amount": 10.0},
        "owner": owner_addr, "agent_id": TRAVEL_AGENT})
    show("v2 生效", v["decision"] == "DENY",
         f"同一笔 $10 被拒（user v2 上限已收紧到 $5、agent 上限 $8，取最严）：{v['reasons'][0]}")

    hr("⑦ 篡改攻击：改动历史版本内容后试图重新提交")
    tampered = new_document(owner_addr, "user", None, 2,
                            "每天所有 Agent 开销不超过 100 美元，单笔不超过 5000 美元；"
                            "只允许付给白名单服务商（airline.example）；"
                            "超过 3 美元要我手机确认", prev.hash)
    tampered.created_at = doc.created_at
    tampered.signature = doc.signature          # 沿用 v2 的签名
    try:
        gw.set_policy(tampered)
        show("篡改版本", False, "被接受了！")
    except PermissionError as e:
        show("篡改版本", True, f"拒绝 — {e}（内容变了，原签名即刻失效）")

    hr("版本链与锚定日志")
    for h in gw.store.history(owner_addr):
        print(f"   {h['scope']:<6} v{h['version']}  hash=…{h['hash'][-12:]}"
              f"  prev=…{h['prev_hash'][-12:]}")
    print("   锚定日志（= 模拟的链上承诺）：")
    for line in anchor.path.read_text().splitlines():
        a = json.loads(line)
        print(f"     v{a['version']} hash=…{a['hash'][-12:]} owner=…{a['owner'][-6:]}")


if __name__ == "__main__":
    main()
