# Boundra · Payment approval gateway for AI agents

<!-- mcp-name: io.boundra/boundra -->

[![Boundra on mcprush — Grade A, Trust 95/100](https://mcprush.com/badge/boundra-payment-approval-gateway-for-ai-agent-mcp/card.svg)](https://mcprush.com/to/boundra-payment-approval-gateway-for-ai-agent-mcp?ref=badge)

> **Boundra** (boundra.io) — *Boundaries for agent payments.*
> The boundary layer for agent payments: before any money moves, answer
> "should this payment happen at all?"

Our thesis: **the opportunity in agent payments is not "how to pay" (protocols
solved that) but "whether to pay."**

One natural-language rule → compiled into a policy → every payment the agent
attempts is genuinely evaluated → allow / ask a human / veto, with a full
audit trail.

Two run modes:

- **Offline simulation** (`demo.py`): stdlib only, self-contained mock merchant
  and wallets, zero dependencies, zero network
- **Real protocol** (`demo/demo_real.py`): standard MCP server + real x402 SDK +
  Base Sepolia testnet

## Data flow

![Boundra data flow](docs/dataflow.png)

Private keys stay with the user; the gateway is read-only; optional on-chain
commits contain hashes, never policy plaintext. Hosted MCP stores the signed
policy document you submit; self-hosted mode keeps it local. Revocation is free
— forever, no exceptions.

> **Integrating Boundra into your agent?** See
> [docs/INTEGRATION.md](docs/INTEGRATION.md) — hosted API, fail-closed Python
> and TypeScript x402 SDKs, two-phase budget settlement, and MCP paths.

## See it run in 60 seconds

```bash
# 1. Hit the hosted evaluation API — a real, public x402 merchant
curl -X POST https://api.boundra.io/evaluate/basic \
  -H 'Content-Type: application/json' \
  -d '{"intent":{"amount":0.01,"recipient":"api.weather.com","purpose":"weather data"}}'
# → {"decision":"ALLOW",...}  ·  semantic layer: 100 free calls per agent,
#   then $0.003 via x402 — settled for real on Base mainnet (USDC)

# 2. Local end-to-end simulation — a data-procurement agent's day
.venv/bin/python demo/simulate_agent.py
# signed policy publish → ALLOW / DENY (limit) / ASK → user-signed approval token
# → prompt-injection blocked → audit trail, plus a dogfood call to api.boundra.io

# 3. One-line MCP install + 2-minute policy wizard
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-mcp     # stdio server
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-init    # wallet → template → sign → anchor
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-approve # sign an ASK challenge
```

## The complete flow, step by step

Zero to a signed, enforced policy in ~10 minutes. No accounts, no dashboards —
your wallet key is your identity.

**0 · Try the hosted API** *(30 seconds, nothing to install)*

```bash
curl -X POST https://api.boundra.io/evaluate/basic \
  -H 'Content-Type: application/json' \
  -d '{"intent":{"amount":0.01,"recipient":"api.weather.com","purpose":"weather data"}}'
```

**1 · Bind your identity — the wallet is the account**

```bash
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-init
```

Creates or imports a Base wallet. No signup, no email — the private key never
leaves your device, and your address *is* your identity everywhere in the
system.

**2 · Set your policy**

The wizard offers templates (personal assistant / travel / dev API /
procurement) or lets you write your own rule in plain English, then compiles
it and shows you exactly what will be enforced — before anything is signed.

**3 · Sign & publish**

You sign the policy document with your wallet key (EIP-191). It is published
as a versioned, hash-chained document. The default commitment is a local hash
log; Base Sepolia anchoring is optional. From this moment the gateway is
read-only for every agent — changing the policy without your key is impossible.

**4 · Mount the MCP server into your agent**

Hosted (zero install, multi-tenant, default-deny until you publish a signed
policy):

```json
{ "mcpServers": { "boundra": { "url": "https://api.boundra.io/mcp" } } }
```

Or self-hosted:

```json
{ "mcpServers": { "boundra": { "command": "uvx",
  "args": ["--from", "git+https://github.com/gobeyondfj-cmd/boundra", "boundra-mcp"] } } }
```

**5 · Every payment gets a verdict before signing**

Your agent calls `evaluate_payment`; the 5-stage pipeline answers
**ALLOW** (sign & settle) / **DENY** (aborted pre-signature, zero on-chain
trace) / **ASK** (a challenge bound to this exact payment).

**6 · When the agent asks, you decide**

```bash
uvx --from git+https://github.com/gobeyondfj-cmd/boundra boundra-approve
```

Paste the ASK challenge, sign it on your device — the agent retries with the
signed approval token. Forged, rebound, or replayed tokens are rejected.

**7 · Watch, update, revoke**

`get_spend_report` / `get_audit_trail` / `get_policy_versions` keep you in
control day to day. Every policy change is a new signed version
(`set_policy`); `revoke_policy` is instant default-deny and **free forever**.

## Quick start (from source)

```bash
cd boundra

# Mode 1: offline simulation (run this first — the full flow in 30 seconds)
python demo/demo.py

# Mode 2: real x402 + MCP
python -m venv .venv && .venv/bin/pip install \
    "x402[evm,httpx]" mcp eth-account fastapi uvicorn
.venv/bin/python tests/test_mcp_client.py    # MCP protocol-level tests
.venv/bin/python demo/setup_wallets.py       # generate Base Sepolia test wallets + faucet guide
.venv/bin/python demo/demo_real.py           # real protocol end-to-end (settles on-chain after funding)
```

## Architecture: where the gateway stands

```
Agent framework (Claude / custom)
   │  MCP protocol (stdio)
   ▼
mcp_server.py  ──→  policy.py 5-stage evaluation pipeline
(policy decision     semantic → whitelist → limit → approval → budget
 point)
   ▲
   │ on_before_payment_creation hook (x402 SDK v2 native interception point)
x402_buyer.py ──sign (EIP-3009)──→ x402 server ──→ facilitator ──→ Base Sepolia
```

**Key finding**: x402 SDK v2 natively provides the `on_before_payment_creation`
hook plus `register_policy` / `set_spend_controls` — the policy layer is not a
bolt-on; it is the slot the official protocol stack reserves. This validates
the wedge.

## File map

```
boundra/
├── policy.py                 intent compilation (NL→Policy, bilingual CN/EN) + 5-stage pipeline + platform red lines
├── signed_policy.py          ★ signed policy documents + version chain + revocation (keccak)
├── approval.py               ★ signed approval tokens (challenge → user signs → gateway verifies)
├── mandate.py                ★ task Mandates: single-use authorization, burned after use (pipeline layer 4)
├── templates.py              ★ policy template library (personal assistant / travel / dev API / procurement)
├── chain_anchor.py           ★ anchor backends: local simulation + real Base Sepolia (web3)
├── gateway.py                gateway core v3: 4-layer stacking + token verification + Mandate + revocation
├── spend_ledger.py           ★ SQLite atomic reservations → settlement / release, with capability tokens
├── usage_metrics.py          persistent hosted evaluation, usage and revenue counters
├── rail_adapters.py          ★ rail adapter layer: evaluation core decoupled from settlement rails (x402 first)
├── boundra_x402.py           ★ async Python client + fail-closed x402 pre-sign hook
├── sdk/typescript/           ★ equivalent TypeScript client and x402 hook, with tests
├── mcp_server.py             ★ standard MCP server (11 tools, stdio)
├── boundra_cli.py            ★ boundra-init (policy wizard) + boundra-approve (challenge signer)
├── contracts/
│   └── PolicyAnchor.sol      ★ on-chain commitment contract (owner-write, anyone-read)
├── demo/
│   ├── simulate_agent.py     ★ end-to-end user journey simulation (MCP + hosted API dogfood)
│   ├── demo_policy.py        signed-policy demo (impersonation / unsigned / tampering attacks all blocked)
│   ├── demo_real.py          real-protocol end-to-end orchestration
│   ├── x402_resource_server.py  real x402 paywall server (FastAPI middleware)
│   ├── x402_buyer.py         real x402 buyer, evaluated by the MCP gateway before signing
│   ├── setup_wallets.py      Base Sepolia test wallet generation
│   └── demo.py / merchant.py / agent.py   offline simulation (zero dependencies)
├── tests/
│   ├── test_product.py       ★ product tests (28 assertions)
│   ├── test_mcp_client.py    MCP protocol-level tests
│   ├── test_reservation_sdk.py  atomic budget, metrics and Python SDK tests
│   ├── test_vectors.py       CC0 policy / ledger / approval consistency vectors
│   ├── vectors/              machine-readable CC0 conformance fixtures
│   └── run_semantic_eval.py  reproducible injection-corpus metrics (no LLM)
├── docs/
│   ├── dataflow.png          data-flow diagram (English)
│   └── render_*.py           diagram renderers (architecture / flow / use-case / data flow)
└── state/audit.jsonl         audit trail (generated at runtime, gitignored)
```

## Signed policies: the root of trust is the user's key, not the gateway's database (v2)

A policy is a **document signed by the user's wallet**, not a row in the
gateway's configuration:

```
user private key signature ──→ PolicyDocument (NL rule + compiled artifact + prev_hash + version)
                      │  three checks: signer == owner, versions sequential, prev_hash unbroken
                      ▼
              PolicyStore (version chain) ──→ LocalAnchor (simulated commitment; production writes to contract)
                      │
                      ▼  check_integrity before every evaluation (signature + anchor)
              5-stage pipeline; audit records cite policy version + hash
```

- **Read-only for agents**: without the user's private key, `set_policy` always
  fails — impersonation, unsigned, and tampered submissions are all blocked
  (seven scenarios in `demo/demo_policy.py`)
- **Two stacked scopes**: user-level (rules for all agents) × agent-level
  (per-agent rules); the strictest verdict wins
- **Every version bump is a signature**: v2 must reference v1's hash; tampering
  with any historical version → signature invalid + chain broken
- **Privacy shape**: optional on-chain commitments contain only hashes. Hosted
  MCP stores the signed policy document submitted by the user; self-hosted mode
  keeps policy plaintext on the user's device

Run: `.venv/bin/python demo/demo_policy.py`

## Verdict credentials: every decision comes with a signed receipt (v4)

Every `evaluate_payment` response carries a `credential` — a
`boundra.verdict/v1` receipt signed by the platform key (EIP-191). It answers
"who can prove this ALLOW / DENY / ASK actually happened, under this policy,
for this exact payment intent?"

- **Binds the intent and expectations**: `intent_hash` covers the legacy
  {amount, recipient, purpose} plus any supplied quote / expectation fields
  (expected amount/payee, resource, method, network, asset). The credential also
  carries explicit `quote`, `expectation`, and `expectation_source` fields
- **Binds the policy**: `policy_refs` lists the policy version hashes used,
  comparable against local or optional on-chain anchors
- **Binds evaluation state**: `audit_id`, `evaluation_id`, and
  `reservation_status` link the decision to its audit and budget lifecycle;
  the secret reservation token is deliberately excluded
- **Verifiable offline, by anyone**: strip `proof`, canonicalize the payload
  (sorted keys, compact separators), recover the EIP-191 signer — it must
  equal `issuer` **and** the platform address
  `0xF30955B6Be768370Da13fca237A5f252F6081d1C` (the same address that receives
  x402 revenue — one identity for signing and settlement). Published at
  `GET https://api.boundra.io/verdict-key`.

```python
from verdict_credential import verify_credential
ok, why = verify_credential(verdict["credential"])   # offline, no API call
```

Self-hosted gateways attach no credential unless `BOUNDRA_VERDICT_KEY` is set —
unsigned mode stays fully backward compatible.

## Two-phase budget accounting

For hosted calls with an `owner`, ALLOW atomically reserves budget in SQLite and
returns `evaluation_id` plus a private `reservation_token`. After the real rail
settles, call `POST /evaluations/{id}/settle` with the transaction reference. If
payment fails or is abandoned, call `/release` to restore the allowance. Tokens
are SHA-256 hashed at rest and never enter audit logs or signed credentials.
Unconfirmed reservations expire after 15 minutes; duplicate settlement is
idempotent and cannot spend the same reservation twice.

For concurrent payment creation, pass a caller-generated top-level `purchase_id`
that identifies one logical purchase before fan-out. Within an owner, only its
first atomic reservation can receive ALLOW. A same-ID replay returns DENY at the
`idempotency` layer with the existing evaluation state and no reservation token;
reusing the ID for different intent fields sets `purchase_id_conflict=true`.
Different purchase IDs remain independent even when their quotes are identical.
Omitting `purchase_id` preserves the original budget-only behavior. This is a
shared pre-sign boundary, not post-sign chain reconciliation: after an ambiguous
payment outcome, reconcile before creating another purchase attempt.

## MCP server tools

| Tool | Purpose | Caller |
|------|---------|--------|
| `evaluate_payment` | Pre-payment evaluation. ALLOW proceeds / DENY aborts / **ASK** returns a challenge the user signs; retry with `approval_token` | Agent |
| `settle_evaluation` / `release_evaluation` | Confirm actual settlement or restore an unused reservation | Agent with capability token |
| `get_evaluation` | Inspect reservation / settlement state | Agent |
| `set_policy` | **Privileged**: submit a new policy version; requires the owner's EIP-191 signature | User only |
| `revoke_policy` | **Privileged**: revoke a policy chain (default-deny immediately, free forever) | User only |
| `get_policy_versions` | Policy version history (chain + hashes, comparable with on-chain commitments) | Anyone |
| `get_policy` | View the legacy fallback policy | Anyone |
| `get_spend_report` | Today's spend / remaining budget | Agent |
| `get_audit_trail` | Approval audit records | Agent |
| `request_policy_change` | Agent asks the user for a policy change (queued, never self-applied) | Agent |
| `list_templates` / `render_template` | Policy template helpers | Agent |

### Mount into Claude Desktop

One line — uvx resolves dependencies and starts the server:

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

On first run, use the **`boundra-init`** wizard (same package): create/import a
wallet → pick a policy template → sign → publish via MCP, about 2 minutes.
When your agent hits ASK, run **`boundra-approve`** and paste the challenge to
produce a signed approval token.

## ⚠️ Known trust-boundary simplifications (must fix for production)

1. ~~`human_approved` self-attested by the agent~~ **solved (v3)**: human
   approval is now a signed approval token (approval.py) — ASK returns a
   challenge bound to the exact payment intent; the user's device signs it;
   the gateway verifies signature + field-by-field intent match + nonce
   replay protection. The legacy self-attested path remains but is tagged
   `approve(legacy-deprecated)`.
2. **MCP-level interception is advisory** — an agent framework can bypass the
   gateway and talk to the wallet directly. Mitigation: pair with x402's
   native hook (demonstrated in `demo/demo_real.py`; the hook fires inside the SDK,
   before signing). Endgame: compile the deterministic parts of policy into a
   smart-contract wallet module, enforced at the settlement layer.
3. **Semantic detection is deterministic regex, not an LLM** — the included
   30-case corpus currently reports 15 TP / 15 TN / 0 FP / 0 FN (baseline before
   rule tuning: 9 TP / 14 TN / 1 FP / 6 FN). This small, self-authored corpus is
   a regression benchmark, not evidence of real-world detection rates. Run
   `.venv/bin/python tests/run_semantic_eval.py` and inspect `evaluations/`.
4. **Anchoring defaults to a local log** — the real contract
   (`contracts/PolicyAnchor.sol`) and web3 client (`chain_anchor.BaseSepoliaAnchor`)
   are ready; set `chain_config.json` to switch to the real chain (deploy via
   Remix with test ETH).

## Economic model: the evaluation API is itself an x402 merchant

Tiered pricing:

| Operation | Price |
|-----------|-------|
| Deterministic evaluation (`/evaluate/basic`) | Free |
| Semantic evaluation (`/evaluate/semantic`) | 100 free calls per agent; then $0.003 USDC/call (real x402 charge) |
| Policy anchoring / revocation | Free (revocation is free forever — a safety red line) |

Open core: the client, evaluation engine, and demos in this repo are
Apache-2.0. The hosted evaluation service (tiered pricing, free-quota
accounting, revenue ledger) runs from the private `boundra-ops` repo.

## Verified / not yet verified

**Verified** (`test_product.py` 28 assertions + MCP protocol tests + 11 CC0
vectors + Python/TypeScript SDK tests): 4-layer policy stacking (platform red
lines × user × agent × Mandate) with strictest-wins; signed approval tokens
(valid passes, forged rejected, intent-rebinding rejected, replay rejected);
Mandate single-use; revocation defaults to deny and a signed v2 restores;
template render+compile; atomic reservation / release / idempotent settlement;
fail-closed SDK hooks; all MCP tools over the wire; gateway blocks an injection
attack inside the real x402 402 round-trip before the SDK signs; the full
tiered-pricing loop — first
proven on Base Sepolia (2026-10-16), then on **Base mainnet (2026-10-21)**:
free tier passes, paid tier settles $0.003 USDC for real (buyer 1.010 → 1.007,
platform address credited 0.003), dogfood hook self-evaluates, semantic layer
blocks the injection, `/revenue` ledger entry.
The mainnet settlement tx is publicly verifiable: [`0xf04d3497…4eac7e5`](https://basescan.org/tx/0xf04d3497626c7eea675440eefd037da22ac400ca874daf8e90564ca124eac7e5)
— the live API now settles on Base mainnet via Coinbase's CDP facilitator.
Earlier testnet proof: [`0x5ad8ef4b…c474bbe`](https://sepolia.basescan.org/tx/0x5ad8ef4b4d6eef7ba34e718742068f8e8480336c12032a3a85801b101c474bbe)
(the hosted service code is closed by design — the chain is the receipt).

**Not yet verified**: real on-chain anchoring (contract ready, needs test ETH
to deploy); real user demand; semantic layer false-positive rate under real
LLM behavior distributions.
