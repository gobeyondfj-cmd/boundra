# -*- coding: utf-8 -*-
"""render_usecase.py — 生成 Agent 支付审批网关的用户用例图（UML use case 风格）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

setup_plot()

import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyArrowPatch, FancyBboxPatch

fig, ax = plt.subplots(figsize=(14.5, 9))
ax.set_xlim(0, 100)
ax.set_ylim(0, 62)
ax.axis("off")

EDGE = {"user": "#1A73E8", "agent": "#D93025", "audit": "#5F6368",
        "uc": "#188038", "support": "#9334E6", "chain": "#B06000"}
FILL = {"uc": "#E6F4EA", "support": "#F3E8FD", "chain": "#FEF7E0"}


def actor(x, y, name, kind, sub=None):
    c = EDGE[kind]
    ax.add_patch(plt.Circle((x, y + 4.6), 1.7, fill=False, ec=c, lw=2))
    ax.plot([x, x], [y + 2.9, y - 1.5], color=c, lw=2)
    ax.plot([x - 2.6, x + 2.6], [y + 1.2, y + 1.2], color=c, lw=2)
    ax.plot([x, x - 2.1], [y - 1.5, y - 4.2], color=c, lw=2)
    ax.plot([x, x + 2.1], [y - 1.5, y - 4.2], color=c, lw=2)
    ax.text(x, y - 6.2, name, ha="center", fontsize=11, weight="bold", color=c)
    if sub:
        ax.text(x, y - 8.2, sub, ha="center", fontsize=8.5, color="#666666")


def usecase(cx, cy, text, kind="uc", w=21, h=7.5, fs=9.5):
    ax.add_patch(Ellipse((cx, cy), w, h, fc=FILL[kind], ec=EDGE[kind], lw=1.5))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs)


def assoc(x1, y1, x2, y2, color="#888888"):
    ax.plot([x1, x2], [y1, y2], color=color, lw=1.2)


def dep(x1, y1, x2, y2, label, color="#9334E6", ly=1.2, lx=0, curve=0.0, fs=8.5):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                                 mutation_scale=12, color=color, lw=1.3,
                                 linestyle=(0, (5, 3)),
                                 connectionstyle=f"arc3,rad={curve}"))
    ax.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, ha="center",
            fontsize=fs, color=color)


# ═══ 系统边界 ═══
ax.add_patch(FancyBboxPatch((17, 2), 66, 57, boxstyle="round,pad=0.3",
                            fc="#FDFDFD", ec="#BBBBBB", lw=1.4, zorder=0))
ax.text(50, 56.5, "Agent 支付审批网关", ha="center", fontsize=13, weight="bold",
        color="#444444")

# ═══ 参与者 ═══
actor(8, 34, "用户", "user", "策略所有者\n（持钱包私钥/passkey）")
actor(92, 42, "AI Agent", "agent", "支付执行者\n（可被注入带偏）")
actor(92, 12, "审计/合规", "audit", "企业审计员\n（只读）")

# ═══ 用例（用户侧 · 左列） ═══
usecase(34, 50, "自然语言定制策略\n（编译+确认回环）")
usecase(34, 40, "签名发布 / 撤销策略版本")
usecase(34, 30, "审批支付请求（人在回路）")
usecase(34, 20, "查看支出与审计轨迹")
usecase(34, 10, "批准 Agent 的预算申请")

# ═══ 用例（Agent 侧 · 右列） ═══
usecase(66, 50, "支付前评估\nevaluate_payment")
usecase(66, 40, "策略验签 + 锚定核对", "support")
usecase(66, 30, "执行 x402 支付结算", "chain")
usecase(66, 20, "申请调整预算 / 策略")

# ═══ 支撑用例（底部中央，减少交叉） ═══
usecase(48, 6, "链上承诺（Base Sepolia）", "chain", w=26)

# ═══ 关联线：用户 ═══
for cy in (50, 40, 30, 20, 10):
    assoc(10.8, 33.2, 23.6, cy + (34 - cy) * 0.06, EDGE["user"])
# ═══ 关联线：Agent ═══
for cy in (50, 30, 20):
    assoc(89.4, 40.5, 76.4, cy + (40.5 - cy) * 0.08, EDGE["agent"])
# ═══ 关联线：审计员 ═══
assoc(89.4, 10.8, 44.6, 18.6, EDGE["audit"])     # → 查看支出与审计
assoc(89.4, 9.8, 61, 6.8, EDGE["audit"])         # → 链上承诺（核对锚点）

# ═══ 依赖关系 ═══
dep(63, 46.4, 63.8, 43.7, "«include»", lx=-10.5)                        # 评估 include 验签
dep(43.5, 36.8, 47, 10.2, "«include»", color="#B06000", lx=-8,
    curve=0.0)                                                          # 发布 include 锚定
dep(45, 31.8, 56.5, 44.8, "«extend»\nASK 时触发", color="#188038",
    lx=-4, curve=0.15)                                                  # 审批 extend 评估
dep(72.5, 46.4, 70.5, 34, "ALLOW 才执行", color="#D93025", lx=10,
    curve=-0.35)                                                        # 评估→执行 前置
dep(55.6, 18.2, 44.4, 11.8, "需用户批准", color="#B06000", lx=0, ly=1.4,
    curve=-0.1)                                                         # 申请→批准

ax.text(50, 60.5, "Agent 支付审批网关 · 用户用例图", ha="center",
        fontsize=16, weight="bold")

out = Path(__file__).parent.parent / "strategy" / "usecase-v1.png"
fig.savefig(out, bbox_inches="tight", dpi=160)
print(f"saved: {out}")
