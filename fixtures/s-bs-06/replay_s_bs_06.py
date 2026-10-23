# -*- coding: utf-8 -*-
"""
replay_s_bs_06.py — Replay igor-vii's S-BS-06 fixtures (x402-foundation/x402#3866)
against the Boundra policy engine (offline, deterministic layer + semantic anchor).

No third-party dependencies. Python 3.10+.

Usage:
    python replay_s_bs_06.py            # human-readable table + cross-checks
    python replay_s_bs_06.py --json     # machine-readable results to stdout
    python replay_s_bs_06.py --json -o results.json

Model:
  - payTo 0x9999...9999 (the fault-injection stand) is whitelisted
  - per_tx_max = $1.00 (mirrors the fixture's spendControls.maxAmountPerPayment)
  - require_approval_above = $1.00 (auto-approve anything at/under the cap)
  - caller expectation anchored at $0.01 (EXPECTED_PRICE_ATOMIC / first accepted quote)
  - max_amount_jump_ratio = 3.0 (Boundra default semantic red line)

For every cycle we evaluate the exact amount the stand served, and compare the
decision with what the official client did (signed or refused). Case values are
transcribed 1:1 from the published fixtures (see README.md for source + pin).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from policy import Policy, PaymentIntent, evaluate, platform_check

PAY_TO = "0x9999999999999999999999999999999999999999"
ATOMIC = 1_000_000  # USDC 6 decimals -> USD

policy = Policy(
    raw_mandate="Pay the weather API $0.01 per call; max $1 per transaction; only pay the known stand address.",
    per_tx_max=1.00,
    daily_budget=10.0,
    allowed_recipients=[PAY_TO],
    require_approval_above=1.00,
    max_amount_jump_ratio=3.0,
)
EXPECTED = 0.01  # caller-supplied expectation, $0.01

# (session, mode, cycle, served_atomic, what the official client did)
CASES = [
    # quote-control fixture
    ("c1_run1",  "observe", 1, 10000,   "signed"),
    ("c1_run1",  "observe", 2, 10000,   "signed"),
    ("c2_run1",  "observe", 1, 10000,   "signed"),
    ("c2_run1",  "observe", 2, 100000,  "signed"),   # quote 10x'd, official client signed anyway
    ("c2ctl_run1", "control", 2, 100000, "rejected by igor's control gate pre-signer"),
    ("e1ctl_run1", "control", 1, 50000,  "rejected: caller_expectation_mismatch (below $1 cap)"),
    # overlimit fixture
    ("h1_run1",  "observe", 1, 5000000, "rejected by $1 spend cap pre-signer"),
    # headline capture from issue #3866
    ("issue-3866-original", "observe", 1, 100000, "signed (under the $1 cap)"),
]


def run_cases():
    results = []
    for sess, mode, cyc, atomic, official in CASES:
        amount = atomic / ATOMIC
        intent = PaymentIntent(
            amount=amount, recipient=PAY_TO,
            purpose="weather API call (S-BS-06 replay)",
            expected_amount=EXPECTED,
        )
        d = platform_check(intent, spent_today=0.0) or evaluate(policy, intent, spent_today=0.0)
        results.append({
            "session": sess, "mode": mode, "cycle": cyc,
            "served_atomic": atomic, "served_usd": amount,
            "boundra_decision": d.action, "boundra_layer": d.layer,
            "reasons": list(d.reasons),
            "official_client": official,
        })
    return results


def cross_checks(results):
    def find(sess, cyc=None):
        return next(r for r in results if r["session"] == sess
                    and (cyc is None or r["cycle"] == cyc))
    return [
        ("c1 both cycles ALLOW (unchanged quote still works)",
         all(r["boundra_decision"] == "ALLOW" for r in results if r["session"] == "c1_run1")),
        ("c2 cycle 2 DENY at semantic layer (official client SIGNED the 10x quote)",
         find("c2_run1", 2)["boundra_decision"] == "DENY"
         and find("c2_run1", 2)["boundra_layer"] == "semantic"),
        ("c2ctl matches igor's control gate: DENY pre-signer",
         find("c2ctl_run1")["boundra_decision"] == "DENY"),
        ("e1 first-acceptance mismatch DENY (below the $1 cap - cap alone misses it)",
         find("e1ctl_run1")["boundra_decision"] == "DENY"),
        ("h1 $5.00 DENY (semantic layer fires before the $1 limit layer)",
         find("h1_run1")["boundra_decision"] == "DENY"),
        ("original #3866 capture: $0.10 under the cap -> Boundra DENYs the signed intent",
         find("issue-3866-original")["boundra_decision"] == "DENY"),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-o", "--output", help="write JSON results to this file")
    args = ap.parse_args()

    results = run_cases()
    checks = cross_checks(results)
    ok = all(passed for _, passed in checks)

    if args.json or args.output:
        payload = {
            "subject_under_test": "Boundra policy engine (policy.evaluate, deterministic + semantic layers)",
            "fixtures": {
                "source": "igor-vii/Argus-Agent-Test-Lab",
                "files": ["s-bs-06-overlimit.fixture.json", "s-bs-06-quote-control.fixture.json"],
                "upstream_issue": "https://github.com/x402-foundation/x402/issues/3866",
                "x402_version_under_test": "@x402/* 2.28.0",
            },
            "config": {
                "per_tx_max_usd": 1.00,
                "caller_expectation_usd": EXPECTED,
                "max_amount_jump_ratio": 3.0,
                "whitelist": [PAY_TO],
            },
            "results": results,
            "cross_checks": [{"check": label, "pass": passed} for label, passed in checks],
            "summary": {"total": len(checks), "passed": sum(1 for _, p in checks if p),
                        "all_pass": ok},
        }
        text = json.dumps(payload, indent=2)
        if args.output:
            Path(args.output).write_text(text + "\n")
            print(f"wrote {args.output}", file=sys.stderr)
        if args.json:
            print(text)
    else:
        print(f"{'session':<22}{'cycle':<7}{'served':>9}  {'Boundra':<8}{'layer':<11} official client / control")
        print("-" * 96)
        for r in results:
            print(f"{r['session']:<22}{r['cycle']:<7}{('$' + format(r['served_usd'], '.2f')):>9}  "
                  f"{r['boundra_decision']:<8}{r['boundra_layer']:<11} {r['official_client']}")
            for reason in r["reasons"]:
                print(f"{'':>40}↳ {reason}")
        print("\n=== Verdict cross-check ===")
        for label, passed in checks:
            print(("PASS " if passed else "FAIL ") + label)
        print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
