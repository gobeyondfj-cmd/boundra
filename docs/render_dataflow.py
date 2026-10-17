# -*- coding: utf-8 -*-
"""render_dataflow.py — Boundra data flow diagram (DFD), English edition"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot
setup_plot()

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(15, 9.5))
ax.set_xlim(0, 100); ax.set_ylim(0, 64); ax.axis("off")
fig.patch.set_facecolor("#0a0f0d"); ax.set_facecolor("#0a0f0d")

C_USER, C_AGENT, C_GW = "#f5c518", "#38bdf8", "#34d399"
C_STORE, C_CHAIN, C_RAIL, C_AUDIT, C_CLOUD = "#a78bfa", "#fb923c", "#f472b6", "#64748b", "#22d3ee"


def box(x, y, w, h, title, sub, color, title_fs=13, sub_fs=9):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.4",
                                fc="#111816", ec=color, lw=1.8))
    ax.text(x + w / 2, y + h - 3.2, title, ha="center", va="center",
            color=color, fontsize=title_fs, fontweight="bold")
    for i, line in enumerate(sub):
        ax.text(x + w / 2, y + h - 6.6 - i * 3.1, line, ha="center", va="center",
                color="#94a3b8", fontsize=sub_fs)


def flow(x1, y1, x2, y2, label, color, rad=0.0, lx=0, ly=0, ls=9):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), connectionstyle=f"arc3,rad={rad}",
                                 arrowstyle="-|>", mutation_scale=16, color=color, lw=1.6))
    mx, my = (x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly
    if rad:
        mx += (y2 - y1) * rad * 0.5
        my -= (x2 - x1) * rad * 0.5
    if label:
        ax.text(mx, my, label, ha="center", va="center", color="#e2e8f0", fontsize=ls,
                bbox=dict(boxstyle="round,pad=0.28", fc="#0a0f0d", ec=color, lw=0.9, alpha=0.95))


ax.text(50, 61.5, "Boundra Data Flow · The journey behind one agent payment",
        ha="center", color="white", fontsize=17, fontweight="bold")
ax.text(50, 58.3, "Data sovereignty: policies & keys stay with the user, the gateway is read-only; only hash commitments go on-chain",
        ha="center", color="#64748b", fontsize=9.5)

# ── entities ─────────────────────────────────────────────────────────
box(2, 38, 18, 15, "User (phone / wallet)",
    ["Holds the private key (root of trust)", "Signs policies & approval tokens", "Can revoke anytime — free, forever"], C_USER)
box(2, 8, 18, 14, "Agent (e.g. DataBot)",
    ["Originates payment intents", "Carries the owner identity", "Retries with approval token"], C_AGENT)
box(31, 24, 23, 20, "Boundra MCP Gateway",
    ["evaluate_payment / set_policy", "5-stage evaluation pipeline", "semantic → whitelist → limit → approval → budget",
     "signature verify + replay protection"], C_GW)
box(61, 40, 18, 14, "Policy Version Chain",
    ["PolicyDocument + signature", "version + prev_hash", "read-only for agents"], C_STORE)
box(84, 40, 15, 14, "Blockchain",
    ["hash commitment anchor", "anyone can verify", "no plaintext on-chain"], C_CHAIN)
box(61, 6, 18, 14, "Settlement Rails",
    ["x402 (EIP-3009)", "cards / local rails", "pluggable rail adapters"], C_RAIL)
box(84, 6, 15, 14, "Evaluation API",
    ["api.boundra.io", "basic: free", "semantic: 100 free calls"], C_CLOUD)
box(31, 4, 23, 9, "Audit Log (local)",
    ["every evaluation: intent + verdict + policy hash"], C_AUDIT)

# ── flows (numbered in time order) ───────────────────────────────────
flow(20, 46, 31, 40, "① signed policy document", C_USER, rad=-0.15, ls=9)
flow(54, 40, 61, 46, "② accepted after verification", C_STORE, ls=8.5)
flow(79, 47, 84, 47, "③ hash commitment", C_CHAIN, ls=8.5)
flow(20, 15, 31, 28, "④ payment intent (amount / recipient / purpose)", C_AGENT, rad=-0.12, ls=9)
flow(54, 33, 61, 14, "⑤ ALLOW → sign & settle", C_RAIL, rad=-0.1)
flow(42, 24, 42, 13, "⑥ all verdicts logged", C_AUDIT, ls=8.5)
# ASK loop
flow(31, 37, 16, 36.5, "⑦ ASK → challenge", C_GW, rad=0.35, lx=-3, ls=9)
flow(16, 34, 31, 30.5, "⑧ signed approval token (5 min, one-time)", C_USER, rad=0.3, lx=-4, ly=-1.5, ls=9)
# hosted dogfood
flow(79, 13, 84, 13, "⑨ dogfood call", C_CLOUD, ls=8.5)
flow(91.5, 20, 91.5, 40, "same engine,\nhosted", C_CLOUD, ls=8)

# revocation channel (safety red line)
flow(11, 38, 11, 22, "", C_USER)
ax.text(11, 30, "⑩ revoke → default-deny instantly", ha="center", va="center", rotation=90,
        color="#fca5a5", fontsize=8.5,
        bbox=dict(boxstyle="round,pad=0.25", fc="#0a0f0d", ec="#fca5a5", lw=0.8))
ax.add_patch(FancyArrowPatch((11, 22), (31, 28), connectionstyle="arc3,rad=0.25",
                             arrowstyle="-|>", mutation_scale=14, color="#fca5a5",
                             lw=1.4, linestyle="--"))

fig.savefig(Path(__file__).parent / "dataflow.png", bbox_inches="tight",
            facecolor=fig.get_facecolor(), dpi=150)
print("saved:", Path(__file__).parent / "dataflow.png")
