from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from policy import PaymentIntent, Policy, semantic_check


def run(corpus_path: Path) -> dict:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    results = []
    counts = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    for case in corpus["cases"]:
        intent = PaymentIntent(amount=1, recipient="merchant.example",
                               purpose="evaluation", source_content=case["content"])
        flagged = semantic_check(Policy(raw_mandate="evaluation"), intent) is not None
        outcome = ("TP" if flagged and case["label"] == "malicious" else
                   "FN" if not flagged and case["label"] == "malicious" else
                   "FP" if flagged else "TN")
        counts[outcome] += 1
        results.append({**case, "flagged": flagged, "outcome": outcome})
    malicious = counts["TP"] + counts["FN"]
    benign = counts["TN"] + counts["FP"]
    total = len(corpus["cases"])
    return {
        "schema": "boundra.semantic-report/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "deterministic regex rules in policy.py (no LLM)",
        "corpus": str(corpus_path.name),
        "cases": total,
        "confusion_matrix": counts,
        "accuracy": round((counts["TP"] + counts["TN"]) / total, 4),
        "false_positive_rate": round(counts["FP"] / benign, 4) if benign else None,
        "false_negative_rate": round(counts["FN"] / malicious, 4) if malicious else None,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=HERE / "benchmarks" / "semantic_corpus.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.corpus)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    summary = {k: report[k] for k in
               ("cases", "confusion_matrix", "accuracy", "false_positive_rate", "false_negative_rate")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
