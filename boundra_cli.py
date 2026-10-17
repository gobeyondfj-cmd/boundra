# -*- coding: utf-8 -*-
"""
boundra_cli.py — 用户侧命令行工具（与 MCP server 同一包分发）

两个入口：
  boundra-init     首次向导：生成/导入钱包 → 选策略模板 → 签名 → 经 MCP 发布上链
  boundra-approve  人在回路：对 ASK challenge 签名，输出批准令牌（粘贴回 Agent 对话）

钱包存于 ~/.boundra/wallet.json（权限 600，私钥永不上传）。
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

BOUNDRA_DIR = Path.home() / ".boundra"
WALLET_FILE = BOUNDRA_DIR / "wallet.json"
GENESIS = "0" * 64


# ---------------------------------------------------------------- 钱包

def load_or_create_wallet() -> tuple[str, str, bool]:
    """返回 (address, private_key, 是否新建)"""
    if WALLET_FILE.exists():
        w = json.loads(WALLET_FILE.read_text())
        return w["address"], w["private_key"], False
    from eth_account import Account
    print("未找到本地钱包。")
    choice = input("  [1] 生成新钱包（推荐）  [2] 导入已有私钥  请选择 [1]: ").strip() or "1"
    if choice == "2":
        key = input("  粘贴私钥（0x…，仅保存在本机 ~/.boundra/）: ").strip()
        acct = Account.from_key(key)
    else:
        acct = Account.create()
    BOUNDRA_DIR.mkdir(parents=True, exist_ok=True)
    WALLET_FILE.write_text(json.dumps(
        {"address": acct.address, "private_key": acct.key.hex(),
         "_warning": "本文件含私钥，勿分享、勿提交 git"}))
    os.chmod(WALLET_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return acct.address, acct.key.hex(), True


# ---------------------------------------------------------------- MCP 客户端

async def _mcp_call(s, tool: str, **args):
    r = await s.call_tool(tool, args)
    payload = r.content[0].text
    return payload if isinstance(payload, (dict, list)) else json.loads(payload)


async def _with_server(fn):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            return await fn(s)


# ---------------------------------------------------------------- boundra-init

def _pick_rule() -> str:
    import templates
    ts = templates.list_templates()
    print("\n策略模板：")
    for i, t in enumerate(ts, 1):
        print(f"  [{i}] {t['id']:<18} {t.get('name', '')} — {t.get('desc', '')[:40]}")
    print("  [c] 自定义自然语言规则")
    choice = input("请选择 [1]: ").strip() or "1"
    if choice.lower() == "c":
        return input("用一句话写下你的规则（中英文均可）: ").strip()
    t = ts[int(choice) - 1 if choice.isdigit() and int(choice) >= 1 else 0]
    wl = input("白名单收款方（逗号分隔，如 api.weather.com, data.news.io）: ").strip()
    whitelist = [x.strip() for x in wl.split(",") if x.strip()]
    daily = input("日预算（美元，回车用模板默认）: ").strip()
    per_tx = input("单笔上限（美元，回车默认）: ").strip()
    ask = input("超过多少美元需人工确认（回车默认）: ").strip()
    kw = {}
    if daily: kw["daily"] = float(daily)
    if per_tx: kw["per_tx"] = float(per_tx)
    if ask: kw["ask_above"] = float(ask)
    return templates.render_template(t["id"], whitelist, **kw)


async def _init_async() -> None:
    from policy import compile_policy
    from signed_policy import new_document, sign_document

    print("=" * 60)
    print("Boundra 初始向导 — 2 分钟完成「我的 Agent 花钱边界」")
    print("=" * 60)
    address, key, is_new = load_or_create_wallet()
    print(f"\n👤 钱包：{address}" + ("（已新建，私钥仅存本机）" if is_new else "（已加载）"))

    nl_rule = _pick_rule()
    c = compile_policy(nl_rule)
    print("\n📜 策略预览：")
    print(f"   原始规则：{nl_rule}")
    print(f"   编译产物：单笔≤${c.per_tx_max}  日预算≤${c.daily_budget}  "
          f"白名单 {len(c.allowed_recipients)} 个  超过 ${c.require_approval_above} 需确认")
    if input("\n确认发布？[Y/n]: ").strip().lower() == "n":
        print("已取消。")
        return

    async def submit(s):
        hist = await _mcp_call(s, "get_policy_versions", owner=address)
        user_versions = [h for h in hist if h.get("scope") == "user" and not h.get("agent_id")]
        version = max((h["version"] for h in user_versions), default=0) + 1
        prev = next((h["hash"] for h in user_versions if h["version"] == version - 1), GENESIS)
        doc = new_document(address, "user", None, version, nl_rule, prev)
        doc.signature = sign_document(doc, key)
        r = await _mcp_call(s, "set_policy", owner=doc.owner, scope=doc.scope,
                            version=doc.version, nl_rule=doc.nl_rule,
                            prev_hash=doc.prev_hash, created_at=doc.created_at,
                            signature=doc.signature)
        return r, version

    result, version = await _with_server(submit)
    if result.get("accepted"):
        print(f"\n✔ 策略 v{version} 已发布并锚定：hash={result['hash'][:24]}…")
        print("  你的 Agent 现在每次付款前都会按这条边界被评估。")
        print("  遇到 ASK 时运行 boundra-approve 粘贴 challenge 即可签名放行。")
    else:
        print(f"\n✗ 发布被拒绝：{result.get('error')}")


def init_main() -> None:
    asyncio.run(_init_async())


# ---------------------------------------------------------------- boundra-approve

def approve_main() -> None:
    from approval import sign_approval
    if not WALLET_FILE.exists():
        print("未找到钱包，请先运行 boundra-init。", file=sys.stderr)
        sys.exit(1)
    w = json.loads(WALLET_FILE.read_text())
    src = sys.argv[1] if len(sys.argv) > 1 else None
    raw = Path(src).read_text() if src and Path(src).exists() else (
        src if src else sys.stdin.read())
    raw = raw.strip()
    # 兼容直接粘贴完整 ASK 裁决（取其中 challenge 字段）
    payload = json.loads(raw)
    challenge = payload.get("challenge", payload)
    token = sign_approval(challenge, w["private_key"])
    print(token)
    print("\n↑ 批准令牌（5 分钟有效、一次性）。让 Agent 携带 approval_token 重调 evaluate_payment。",
          file=sys.stderr)


if __name__ == "__main__":
    init_main()
