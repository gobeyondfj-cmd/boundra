# PR Sketch — `expectedAmount`: caller-anchored price expectation for the x402 client SDK

**Status:** posted to [x402#3866](https://github.com/x402-foundation/x402/issues/3866) for evaluation (2026-10-23)
**Target repo:** x402-foundation/x402 (TypeScript client, `@x402/core` + `@x402/http`)
**Context:** [issue #3866](https://github.com/x402-foundation/x402/issues/3866) — the official client signs whatever amount the server declares in the 402 response, including a 10× mid-session hike; `spendControls.maxAmountPerPayment` does not cover below-cap inflation. Verified against igor-vii's S-BS-06 fixtures with a second independent implementation ([comment](https://github.com/x402-foundation/x402/issues/3866#issuecomment-6336282071)).

---

## 1. Problem

The 402 amount is **server-declared**. The client's only built-in guard is `SpendControls.maxAmountPerPayment` — an absolute cap. Anything at or under the cap is signed unconditionally:

- Original #3866 capture: quote jumps $0.01 → $0.10 (10×), under a $1 cap → **signed**.
- S-BS-06 e1: first-ever quote $0.05 vs. caller expectation $0.01, under the cap → **signed** (only igor-vii's out-of-tree control gate catches it).
- S-BS-06 c2: accepted at $0.01 in cycle 1, raised to $0.10 in cycle 2 → **signed** by the observe-mode client.

The cap answers "*never pay more than $X*". It cannot answer "*this isn't the price I agreed to*". Those are different rules — confirmed empirically: the overlimit probe (h1, $5) is caught by both, but the below-cap cases (e1, c2, original capture) pass the cap and only fail an expectation check.

## 2. Proposal

Add a caller-anchored expectation to client spend controls, enforced **pre-signature**, at the same point where `spendControls` filtering already rejects requirements.

### 2.1 API surface (`@x402/core/src/client/x402Client.ts`)

```ts
export interface SpendControls {
  maxAmountPerPayment?: Money | false;
  allowedAssets?: true | SpendControlAsset[];

  /**
   * NEW: caller's expected price. If set, any offered requirement whose
   * amount differs from the expectation is rejected before signing.
   *
   * - Explicit value: `expectedAmount: "$0.01"` (or atomic string per asset)
   * - `"first-accepted"`: anchor on the first ACCEPTED requirement per
   *   quote key (resource url + method + scheme + network + asset + payTo);
   *   subsequent changes are rejected. (Quote ledger — see 2.2.)
   * - `false` / unset: current behavior (default, fully backward compatible).
   */
  expectedAmount?: Money | "first-accepted" | false;

  /**
   * NEW: tolerance for legitimate repricing, as a multiplier on the
   * expectation. `1.0` = exact match only. Default: `1.0` when
   * expectedAmount is set.
   */
  expectedAmountTolerance?: number;
}
```

### 2.2 Quote ledger (for `"first-accepted"` mode)

A small in-memory map on `x402Client`, keyed by the same tuple igor-vii's fixture spec uses:

```
quoteKey = (resourceUrl, method, scheme, network, asset, payTo)
```

- On a **confirmed paid cycle** (settle success), record `acceptedAt` + amount.
- On any later 402 for the same key, compare offered amount against the ledger entry × tolerance.
- Mismatch → reject **before** payload creation, with a typed error:

```ts
class QuoteMismatchError extends Error {
  quoteKey: QuoteKey;
  expected: string;   // atomic
  offered: string;    // atomic
  source: "caller-config" | "quote-ledger";
}
// message mirrors the existing spendControls rejection style:
// "quote-check-rejected: quote_mismatch_vs_accepted: amount 10000 -> 100000"
```

### 2.3 Enforcement point

`x402Client.createPaymentPayload()` already filters candidate requirements through spend controls and throws `Failed to create payment payload: All payment requirements were rejected by spendControls…`. The expectation check slots into the same filter pass, running **before policy functions and before the signer**. No wire-format change, no facilitator change, no server change.

### 2.4 Failure semantics

Fail **closed** and **loud**: reject + throw, never silently pay. Rationale: a quote change mid-session is either a benign reprice (caller should re-confirm explicitly, e.g. surface to the human/LLM one level up) or an attack. Silent acceptance is never the right default for money.

## 3. Interaction with existing hooks

`x402HTTPClient.onPaymentRequired` already lets users bolt this on out-of-tree (igor-vii's control gate does exactly that, and Boundra's gateway hook does it one layer deeper). The argument for first-class SDK support:

1. **Discoverability** — the #3866 reporter assumed the cap covered this; most developers will not write a custom hook.
2. **Fail-safe default placement** — hooks are advisory and can be forgotten per-client; spendControls already run unconditionally (`false` to opt out).
3. **Shared semantics** — a standard `QuoteMismatchError` and quote-key definition lets tooling (test harnesses like Argus, policy gateways like Boundra) interoperate instead of each inventing their own.

## 4. Test plan (adapted from S-BS-06 fixtures)

| Scenario | Setup | Expected |
|---|---|---|
| Unchanged quote | 10000 → 10000 | both cycles pay |
| Mid-session hike | 10000 → 100000, `expectedAmount: "first-accepted"` | cycle 2 rejected pre-signer, `QuoteMismatchError` |
| First-acceptance mismatch | offered 50000, `expectedAmount: "$0.01"` | rejected pre-signer, `source: "caller-config"` |
| Below-cap mismatch | offered 100000, cap $1, expectation $0.01 | rejected (cap alone would pass it) |
| Tolerance | expectation $0.01, tolerance 1.5, offered $0.012 | accepted |
| Disabled | `expectedAmount: false` | current behavior, all sign |

Fixtures: igor-vii/Argus-Agent-Test-Lab `s-bs-06-*.fixture.json` (pinned @x402/* 2.28.0) — already replayed against an independent implementation with matching verdicts.

## 5. Backward compatibility

Fully opt-in; default behavior unchanged. No changes to `PaymentRequired`, `PaymentPayload`, facilitator API, or server SDKs.

## 6. Naming alternatives considered

- `maxPriceDeviation` — implies relative-only; we want both absolute expectation and ledger modes.
- Piggybacking on `allowedAssets[].maxAmountPerPayment` — wrong axis (absolute cap, not expectation), same gap remains.
- Server-signed price commitments — the real endgame (a quote the server can't silently revise), but requires protocol changes; this proposal is client-side and shippable today.

---

*Drafted by Boundra. Cross-implementation evidence: [S-BS-06 replay](https://github.com/gobeyondfj-cmd/boundra/tree/main/fixtures/s-bs-06) (script, pinned config, machine-readable results). Offered for evaluation against the published cases — not a request for commitment.*
