# -*- coding: utf-8 -*-
"""
boundra_cli.py — user-side CLI (shipped in the same package as the MCP server)

Two entry points:
  boundra-init     first-run wizard: create/import wallet → pick a policy template
                   → sign → publish via MCP (version chain + anchor)
  boundra-approve  human-in-the-loop: sign an ASK challenge, print the approval
                   token (paste it back to your agent)

Wallet lives at ~/.boundra/wallet.json (mode 600, key never leaves the device).
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


# ---------------------------------------------------------------- wallet

def load_or_create_wallet() -> tuple[str, str, bool]:
    """Return (address, private_key, was_created)."""
    if WALLET_FILE.exists():
        w = json.loads(WALLET_FILE.read_text())
        return w["address"], w["private_key"], False
    from eth_account import Account
    print("No local wallet found.")
    choice = input("  [1] Create a new wallet (recommended)  [2] Import an existing private key  [1]: ").strip() or "1"
    if choice == "2":
        key = input("  Paste private key (0x…, stored only on this machine at ~/.boundra/): ").strip()
        acct = Account.from_key(key)
    else:
        acct = Account.create()
    BOUNDRA_DIR.mkdir(parents=True, exist_ok=True)
    WALLET_FILE.write_text(json.dumps(
        {"address": acct.address, "private_key": acct.key.hex(),
         "_warning": "contains a private key — never share, never commit"}))
    os.chmod(WALLET_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return acct.address, acct.key.hex(), True


# ---------------------------------------------------------------- MCP client

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
    print("\nPolicy templates:")
    for i, t in enumerate(ts, 1):
        print(f"  [{i}] {t['id']:<18} {t.get('name', '')} — {t.get('desc', '')[:40]}")
    print("  [c] Custom natural-language rule")
    choice = input("Choose [1]: ").strip() or "1"
    if choice.lower() == "c":
        return input("Write your rule in one sentence (English or Chinese): ").strip()
    t = ts[int(choice) - 1 if choice.isdigit() and int(choice) >= 1 else 0]
    wl = input("Whitelisted recipients (comma-separated, e.g. api.weather.com, data.news.io): ").strip()
    whitelist = [x.strip() for x in wl.split(",") if x.strip()]
    daily = input("Daily budget in USD (Enter for template default): ").strip()
    per_tx = input("Per-transaction cap in USD (Enter for default): ").strip()
    ask = input("Require approval above how many USD (Enter for default): ").strip()
    kw = {}
    if daily: kw["daily"] = float(daily)
    if per_tx: kw["per_tx"] = float(per_tx)
    if ask: kw["ask_above"] = float(ask)
    return templates.render_template(t["id"], whitelist, **kw)


async def _init_async() -> None:
    from policy import compile_policy
    from signed_policy import new_document, sign_document

    print("=" * 60)
    print("Boundra setup wizard — define your agent's spending boundaries in 2 minutes")
    print("=" * 60)
    address, key, is_new = load_or_create_wallet()
    print(f"\n👤 Wallet: {address}" + (" (created; key stored locally)" if is_new else " (loaded)"))

    nl_rule = _pick_rule()
    c = compile_policy(nl_rule)
    print("\n📜 Policy preview:")
    print(f"   raw rule : {nl_rule}")
    print(f"   compiled : per-tx ≤ ${c.per_tx_max} · daily ≤ ${c.daily_budget} · "
          f"whitelist {len(c.allowed_recipients)} · approval above ${c.require_approval_above}")
    if input("\nPublish? [Y/n]: ").strip().lower() == "n":
        print("Cancelled.")
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
        print(f"\n✔ Policy v{version} published and anchored: hash={result['hash'][:24]}…")
        print("  Your agent is now evaluated against this boundary before every payment.")
        print("  When it hits ASK, run boundra-approve and paste the challenge to sign.")
    else:
        print(f"\n✗ Publish rejected: {result.get('error')}")


def init_main() -> None:
    asyncio.run(_init_async())


# ---------------------------------------------------------------- boundra-approve

def approve_main() -> None:
    from approval import sign_approval
    if not WALLET_FILE.exists():
        print("No wallet found — run boundra-init first.", file=sys.stderr)
        sys.exit(1)
    w = json.loads(WALLET_FILE.read_text())
    src = sys.argv[1] if len(sys.argv) > 1 else None
    raw = Path(src).read_text() if src and Path(src).exists() else (
        src if src else sys.stdin.read())
    raw = raw.strip()
    # Accepts either a bare challenge or the full ASK verdict (uses its challenge field)
    payload = json.loads(raw)
    challenge = payload.get("challenge", payload)
    token = sign_approval(challenge, w["private_key"])
    print(token)
    print("\n↑ approval token (valid 5 min, single-use). Have the agent retry evaluate_payment "
          "with approval_token.", file=sys.stderr)


if __name__ == "__main__":
    init_main()
