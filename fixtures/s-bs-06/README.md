# S-BS-06 fixture replay — Boundra policy engine

Independent replay of igor-vii's S-BS-06 fixtures
([x402-foundation/x402#3866](https://github.com/x402-foundation/x402/issues/3866))
against Boundra's pre-signature policy engine.

**No third-party dependencies. Python 3.10+.**

## Exact reproduction steps

```bash
git clone https://github.com/gobeyondfj-cmd/boundra
cd boundra/fixtures/s-bs-06

python3 replay_s_bs_06.py                    # human-readable table + cross-checks
python3 replay_s_bs_06.py --json             # machine-readable results to stdout
python3 replay_s_bs_06.py --json -o results.json
```

Exit code `0` = all cross-checks pass. `results.json` (committed) is the output
of the last command on Python 3.12 — every scenario with its decision, the
policy layer that produced it, and the machine-readable reason string.

## Fixture source

`s-bs-06-overlimit.fixture.json` and `s-bs-06-quote-control.fixture.json` are
unmodified copies of the fixtures published by **igor-vii** (Argus-Agent-Test-Lab),
captured 2026-10-21 on Base Sepolia against `@x402/* 2.28.0`. Case values in the
replay script (`CASES`) are transcribed 1:1 from the signer-boundary captures in
those files; the files are included here so the mapping can be audited.

## Configuration (mirrors the fixture setup)

| Parameter | Value | Fixture counterpart |
|---|---|---|
| `per_tx_max` | $1.00 | `spendControls.maxAmountPerPayment: $1` |
| caller expectation anchor | $0.01 | `EXPECTED_PRICE_ATOMIC=10000` / first accepted quote |
| `max_amount_jump_ratio` | 3.0 (Boundra default semantic red line) | — (igor's quote-check gate) |
| whitelist | `0x9999…9999` (the stand) | — |

## Results summary (6/6 cross-checks pass)

| Session | Cycle | Served | Official client | Boundra | Layer |
|---|---|---|---|---|---|
| c1_run1 | 1–2 | $0.01 → $0.01 | signed, signed | ALLOW, ALLOW | pass |
| c2_run1 | 1 | $0.01 | signed | ALLOW | pass |
| c2_run1 | 2 | $0.10 (10× quote hike) | **signed** | **DENY** | semantic |
| c2ctl_run1 | 2 | $0.10 | rejected pre-signer by igor's control gate | **DENY** | semantic — same verdict |
| e1ctl_run1 | 1 | $0.05 (5× expectation, **under the $1 cap**) | rejected: `caller_expectation_mismatch` | **DENY** | semantic — same verdict |
| h1_run1 | 1 | $5.00 | rejected by $1 spend cap | **DENY** | semantic fires first |
| #3866 original capture | 1 | $0.10 (under the cap) | **signed** | **DENY** | semantic |

Settlement is mocked in the fixtures (captures establish signing, not on-chain
transfer). Boundra's evaluation happens strictly pre-signature, so mocked
settlement does not affect these verdicts.

## Companion proposal

[`expected-amount-sketch.md`](expected-amount-sketch.md) — the minimal client-SDK
change (`expectedAmount` + quote ledger) that would make this expectation check
a first-class, fail-closed guard in the official x402 client.
