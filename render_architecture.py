# -*- coding: utf-8 -*-
"""render_architecture.py — 生成 Agent 支付审批网关系统架构图"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

setup_plot()

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

fig, ax = plt.subplots(figsize=(15, 9.5))
ax.set_xlim(0, 100)
ax.set_ylim(0, 64)
ax.axis("off")

C = {
    "user":  ("#E8F0FE", "#1A73E8"),
    "agent": ("#FCE8E6", "#D93025"),
    "gw":    ("#E6F4EA", "#188038"),
    "chain": ("#FEF7E0", "#B06000"),
    "eval":  ("#F3E8FD", "#9334E6"),
    "audit": ("#E8EAED", "#5F6368"),
}


def box(x, y, w, h, text, kind, fs=10.5, bold=False, sub=None, subfs=8.5):
    fc, ec = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.2",
                                fc=fc, ec=ec, lw=1.6))
    weight = "bold" if bold else "normal"
    if sub:
        ax.text(x + w / 2, y + h / 2 + 1.6, text, ha="center", va="center",
                fontsize=fs, weight=weight)
        ax.text(x + w / 2, y + h / 2 - 2.2, sub, ha="center", va="center",
                fontsize=subfs, color="#444444")
    else:
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
                fontsize=fs, weight=weight)


def arrow(x1, y1, x2, y2, label=None, color="#555555", style="-|>",
          ls="-", fs=8.5, lx=0, ly=1.2, curve=0.0):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                 mutation_scale=13, color=color, lw=1.4,
                                 linestyle=ls,
                                 connectionstyle=f"arc3,rad={curve}"))
    if label:
        ax.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, ha="center",
                fontsize=fs, color=color)


# ═══ 泳道背景 ═══
for y0, h, name in [(47, 15, "信任根层（用户自持密钥）"), (24, 21, "网关层（MCP 服务 · 链下评估）"),
                    (2, 20, "结算层（x402 协议 · Base Sepolia）")]:
    ax.add_patch(FancyBboxPatch((0.5, y0), 99, h, boxstyle="round,pad=0.2",
                                fc="#FAFAFA", ec="#DDDDDD", lw=1, zorder=0))
    ax.text(1.8, y0 + h - 1.6, name, fontsize=9, color="#888888", va="center")

# ═══ 信任根层 ═══
box(4, 51, 14, 7, "用户", "user", bold=True, sub="钱包私钥 / passkey")
box(24, 51, 22, 7, "策略文档 v1…vN", "user", bold=True,
    sub="NL规则+编译产物+prev_hash · EIP-191签名")
box(52, 51, 20, 7, "PolicyStore 版本链", "user", bold=True,
    sub="验签 · 版本连续 · prev_hash 不断链")
box(78, 51, 18, 7, "链上承诺（哈希）", "chain", bold=True,
    sub="anchor(hash,version) 仅owner可写")

arrow(18.5, 54.5, 23.5, 54.5, "签名")
arrow(46.5, 54.5, 51.5, 54.5, "提交")
arrow(72.5, 54.5, 77.5, 54.5, "只存哈希承诺")
arrow(11, 50.6, 11, 42.5, "set_policy（特权）", lx=8.5)

# ═══ 网关层 ═══
box(4, 28, 18, 10, "MCP Server（6 工具）", "gw", bold=True,
    sub="evaluate_payment / set_policy(仅用户)\nget_policy_versions / audit …")
box(26, 28, 16, 10, "策略体检", "gw", bold=True, sub="每次评估前\n验签 + 锚定核对")
box(46, 26, 34, 14, None, "eval")
ax.text(63, 37.2, "五段式评估管线（取最严：user层 × agent层 叠加）",
        ha="center", fontsize=10.5, weight="bold", color="#9334E6")
stages = ["① 语义层\n注入/意图漂移", "② 白名单\n收款方", "③ 限额\n单笔",
          "④ 分级审批\nASK转人工", "⑤ 预算\n日累计"]
for i, s in enumerate(stages):
    box(47.5 + i * 6.6, 27.5, 5.8, 7.5, s, "eval", fs=8)
    if i:
        arrow(47.5 + i * 6.6 - 0.9, 31.2, 47.5 + i * 6.6 - 0.1, 31.2, color="#9334E6")
box(84, 28, 13, 10, "账本 + 审计", "audit", bold=True,
    sub="audit.jsonl\n引用策略版本+哈希")

arrow(22.5, 33, 25.5, 33, "")
arrow(42.5, 33, 45.5, 33, "策略可信")
arrow(80.5, 33, 83.5, 33, "裁决留痕")
arrow(62, 50.6, 62, 40.6, "读取激活策略", ls="--", color="#1A73E8")
arrow(87, 50.6, 38, 38.4, "锚定核对", ls="--", color="#B06000", curve=-0.25)

# ═══ 结算层 ═══
box(4, 6, 16, 10, "Agent 框架", "agent", bold=True, sub="Claude / 自研 Agent\n（规划器，可被注入带偏）")
box(24, 6, 18, 10, "x402 SDK 钩子", "agent", bold=True,
    sub="on_before_payment_creation\n签名前拦截点（SDK原生）")
box(46, 6, 14, 10, "商户服务端", "chain", bold=True, sub="HTTP 402\n付款要求")
box(64, 6, 14, 10, "Facilitator", "chain", bold=True, sub="验签 + 代付gas\n链上结算")
box(82, 6, 15, 10, "Base Sepolia", "chain", bold=True, sub="USDC (EIP-3009)\n测试网")

arrow(12, 16.4, 12, 27.6, "MCP(stdio)", lx=-7)
arrow(20.5, 11, 23.5, 11, "402")
arrow(33, 16.4, 20, 27.6, "支付意图", lx=-6.5, curve=-0.15)
arrow(38, 27.6, 33, 16.4, "ALLOW/DENY/ASK", lx=8.5, curve=-0.15)
arrow(42.5, 11, 45.5, 11, "EIP-3009 签名", ly=1.4)
arrow(60.5, 11, 63.5, 11, "")
arrow(78.5, 11, 81.5, 11, "")
arrow(20, 38.4, 10, 50.6, "ASK → 对话确认 → human_approved", ls="--",
      color="#188038", lx=-13, curve=0.3)

ax.text(50, 63.2, "Agent 支付审批网关 · 系统架构", ha="center", fontsize=17, weight="bold")
ax.text(50, 60.8, "签名策略（信任根=用户密钥） × MCP 策略决策点 × x402 原生钩子",
        ha="center", fontsize=11, color="#666666")

out = Path(__file__).parent.parent / "strategy" / "architecture-v2.png"
fig.savefig(out, bbox_inches="tight", dpi=160)
print(f"saved: {out}")
