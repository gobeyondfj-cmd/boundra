# Boundra Integration Guide

Five minutes from zero to an agent whose payments answer to a policy —
**allow, ask, or veto, before the signature.**

Three ways to integrate, in order of effort:

| Path | Effort | Best for |
|---|---|---|
| [A · Hosted evaluation API](#a--hosted-evaluation-api) | 1 line | Any agent, any language |
| [B · Inside the x402 SDK hook](#b--policy-inside-the-x402-sdk-recommended) | ~20 lines | Agents that pay via x402 |
| [C · MCP server](#c--mcp-server) | 1 config block | Claude / Kimi / other MCP hosts |

Identity is your wallet key. No accounts, no API keys, no dashboards.

---

## A · Hosted evaluation API

`POST https://api.boundra.io/evaluate/basic` is free, forever — deterministic
checks (limits, allowlists, daily budgets, revocation):

```bash
curl -X POST https://api.boundra.io/evaluate/basic \
  -H 'Content-Type: application/json' \
  -d '{"intent":{"amount":0.01,"recipient":"api.weather.com","purpose":"weather data"}}'
# → {"decision":"ALLOW","layer":"deterministic","reasons":[],"audit_id":"…",…}
```

`POST /evaluate/semantic` adds the semantic layer (prompt-injection and
intent-drift detection). Send an `x-boundra-agent` header to identify your
agent — identified agents get **100 free semantic calls**, after which the
endpoint charges **$0.003 USDC on Base mainnet via x402**. Anonymous calls
(no header) go straight to the paid path.

```bash
curl -X POST https://api.boundra.io/evaluate/semantic \
  -H 'Content-Type: application/json' \
  -H 'x-boundra-agent: my-data-bot-v1' \
  -d '{"intent":{"amount":0.5,"recipient":"api.example.com","purpose":"buy dataset",
        "source_content":"<merchant response that triggered this payment>"}}'
```

Decisions:

- **ALLOW** — proceed to signing
- **ASK** — a human must sign an approval token for this exact intent
- **DENY** — abort. Nothing was signed, nothing settles, zero on-chain trace

## B · Policy inside the x402 SDK (recommended)

If your agent pays with the x402 Python SDK, run Boundra **inside the
payment-creation hook** — the verdict arrives after the 402 challenge but
*before your agent signs anything*:

```python
import httpx
from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas.hooks import AbortResult

account = Account.from_key(YOUR_AGENT_PRIVATE_KEY)
client = x402Client()
register_exact_evm_client(client, EthAccountSigner(account), networks="eip155:8453")

async def boundra_gate(ctx):
    req = ctx.selected_requirements
    amount_usd = int(req.amount) / 1e6
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            "https://api.boundra.io/evaluate/basic",          # free
            json={"intent": {"amount": amount_usd,
                             "recipient": req.pay_to,
                             "purpose": "x402 payment via Boundra-gated agent"}},
        )
    verdict = r.json()
    if verdict.get("decision") == "ALLOW":
        return None                                          # proceed to signing
    return AbortResult(reason="boundra",
                       message="; ".join(verdict.get("reasons") or ["DENY"]))

client.on_before_payment_creation(boundra_gate)

async with wrapHttpxWithPayment(client) as http:
    resp = await http.get("https://any-x402-merchant.example/paid-endpoint")
```

Use `/evaluate/semantic` instead of `/basic` when you also want the merchant's
response content scanned for injection (pass it as `source_content`).

> **About ASK:** the hosted default policy asks a human whenever the recipient
> is not on the whitelist — paying an arbitrary x402 merchant will return
> `ASK`, not `ALLOW`. That is the system working as designed. Handle it by
> either collecting a [signed approval token](../README.md#the-complete-flow-step-by-step)
> (`boundra-approve`), or by running the MCP path below and ALLOW-listing your
> known merchants in your own signed policy.

## C · MCP server

### C1 · Hosted MCP (zero install)

Point any streamable-HTTP MCP client at our hosted multi-tenant endpoint:

```json
{
  "mcpServers": {
    "boundra": {
      "url": "https://api.boundra.io/mcp"
    }
  }
}
```

Multi-tenant by design: every tool call is namespaced by your wallet
(`owner`). No signed policy on file → **default-deny**. Publish your policy
with `set_policy` (signed by the owner's key), then `evaluate_payment` before
every spend. Spend counters are per-owner; policies persist across restarts.

### C2 · Self-hosted MCP (full policy lifecycle locally)

Full policy lifecycle — signed policies, templates, ASK approval tokens,
spend reports, audit trail — as MCP tools inside your agent host:

```json
{
  "mcpServers": {
    "boundra": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gobeyondfj-cmd/boundra", "boundra-mcp"]
    }
  }
}
```

Then run the 2-minute wizard to bind a wallet, pick a policy template, and
sign it:

```bash
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-init     # wallet → template → sign → anchor
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-approve  # sign an ASK challenge
```

Available tools: `set_policy` · `evaluate_payment` · `get_policy` ·
`revoke_policy` (free forever) · `get_spend_report` · `get_audit_trail` ·
`get_policy_versions` — see the [README](../README.md#mcp-server-tools).

---

## Pricing & networks

| Operation | Price |
|---|---|
| `POST /evaluate/basic` | Free |
| `POST /evaluate/semantic` | 100 free calls per agent, then $0.003 USDC/call (real x402 charge) |
| Policy anchoring / revocation | Free — revocation is free forever, a safety red line |

Settlement: Base mainnet (`eip155:8453`), USDC, via Coinbase's CDP
facilitator. Buyers need USDC only — EIP-3009 means no ETH for gas.

Proof: the API is itself an x402 merchant and we settle on it for real —
[basescan.org/tx/0xf04d3497…](https://basescan.org/tx/0xf04d3497626c7eea675440eefd037da22ac400ca874daf8e90564ca124eac7e5)

---

## Verifying verdict credentials

Every verdict from the hosted API and hosted MCP carries a
`boundra.verdict/v1` credential signed by the platform key (EIP-191).
Verify it offline — no API call, no trust in the transport:

```python
import json, hashlib
from eth_account import Account
from eth_account.messages import encode_defunct

PLATFORM = "0xF30955B6Be768370Da13fca237A5f252F6081d1C"  # GET /verdict-key

def verify(credential: dict) -> bool:
    proof = credential.get("proof") or {}
    payload = {k: v for k, v in credential.items() if k != "proof"}
    canonical = json.dumps(payload, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    signer = Account.recover_message(
        encode_defunct(text=canonical),
        signature=bytes.fromhex(proof["signature"].removeprefix("0x")))
    return signer.lower() == PLATFORM.lower() == credential["issuer"].lower()
```

The credential binds the exact payment intent (`intent_hash` = sha256 over
canonical {amount, recipient, purpose}), the policy versions used
(`policy_refs`), and the server-side audit entry (`audit_id`). Re-attaching it
to a different payment or tampering with any field breaks the signature.

The platform issuer address is published at
`GET https://api.boundra.io/verdict-key` and is the same address that receives
x402 settlement — one identity for signing and revenue.

Questions: support@boundra.io
