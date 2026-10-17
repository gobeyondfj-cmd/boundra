# -*- coding: utf-8 -*-
"""render_flow.py — 生成整体使用流程图（泳道时序图）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

setup_plot()

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

fig, ax = plt.subplots(figsize=(15, 10.5))
ax.set_xlim(0, 100)
ax.set_ylim(0, 72)
ax.axis("off")

C = {"user": "#1A73E8", "agent": "#D93025", "gw": "#188038",
     "merch": "#B06000", "chain": "#9334E6"}

LANES = [(13, "用户", "user", "（含钱包/passkey）"),
         (34, "AI Agent", "agent", ""),
         (54, "网关（MCP）", "gw", ""),
         (73, "商户", "merch", ""),
         (90, "Facilitator/链", "chain", "Base Sepolia")]
X = {k: x for x, _, k, _ in LANES}

# ═══ 泳道头 + 生命线 ═══
for x, name, kind, sub in LANES:
    c = C[kind]
    ax.add_patch(FancyBboxPatch((x - 8, 63.5), 16, 4.6, boxstyle="round,pad=0.3",
                                fc="white", ec=c, lw=1.6, zorder=3))
    label = name if not sub else f"{name}\n{sub}"
    ax.text(x, 65.8, label, ha="center", va="center", fontsize=9.5,
            weight="bold", color=c, zorder=4)
    ax.plot([x, x], [2, 63.5], color=c, lw=1.1, linestyle=(0, (4, 3)), alpha=0.5)

# ═══ 阶段分隔带 ═══
PHASES = [(63.4, 44.5, "阶段一 · 一次性设置"), (44.5, 20, "阶段二 · 日常支付（自动）"),
          (20, 10.5, "阶段三 · 例外分支（人在回路）"), (10.5, 1, "阶段四 · 审计复盘")]
for y1, y0, name in PHASES:
    ax.add_patch(FancyBboxPatch((0.5, y0), 7.5, y1 - y0, boxstyle="round,pad=0.2",
                                fc="#F5F5F5", ec="#DDDDDD", zorder=0))
    ax.text(4.25, (y0 + y1) / 2, name.replace(" · ", "\n"), ha="center",
            va="center", fontsize=8.5, color="#666666", rotation=90)
    ax.plot([9, 99], [y0, y0], color="#DDDDDD", lw=0.8, zorder=0)


def msg(y, a, b, num, text, dashed=False, color=None, ly=0.9):
    c = color or C[{X["user"]: "user", X["agent"]: "agent", X["gw"]: "gw",
                    X["merch"]: "merch", X["chain"]: "chain"}[a]]
    ax.add_patch(FancyArrowPatch((a, y), (b, y), arrowstyle="-|>",
                                 mutation_scale=12, color=c, lw=1.5,
                                 linestyle=(0, (5, 3)) if dashed else "-"))
    direction = 1 if b > a else -1
    ax.text((a + b) / 2, y + ly, f"{num}  {text}", ha="center",
            fontsize=8.8, color="#333333",
            bbox=dict(fc="white", ec="none", pad=0.6))


def selfnote(y, lane, num, text, color):
    ax.add_patch(FancyBboxPatch((lane + 1.5, y - 2), 24, 3.6,
                                boxstyle="round,pad=0.25", fc="#FAFAFA",
                                ec=color, lw=1.2))
    ax.text(lane + 13.5, y - 0.2, f"{num}  {text}", ha="center", va="center",
            fontsize=8.8, color="#333333")

U, A, G, M, H = X["user"], X["agent"], X["gw"], X["merch"], X["chain"]

# ═══ 阶段一 · 一次性设置 ═══
msg(61.5, U, G, "①", "自然语言规则（“旅行Agent每天≤$60…”）")
msg(59.3, G, U, "②", "LLM 编译回显：“我理解为你要求…对吗？”", dashed=True)
msg(57.1, U, G, "③", "确认无误 → 钱包签名 → set_policy 提交 v1")
msg(54.9, G, H, "④", "anchor(hash,v1) 链上承诺", dashed=True)
msg(52.7, U, A, "⑤", "配置 MCP + 绑定 Agent 身份", dashed=True)
selfnote(48.5, G, "", "三道校验：签名者==owner · 版本连续 · prev_hash不断链", C["gw"])

# ═══ 阶段二 · 日常支付 ═══
msg(42.5, A, M, "⑥", "请求资源（API 调用）")
msg(40.3, M, A, "⑦", "HTTP 402 + 付款要求", dashed=True)
msg(38.1, A, G, "⑧", "evaluate_payment（MCP）")
selfnote(34.8, G, "⑨", "策略体检（验签+锚定核对）→ 五段评估管线", C["gw"])
msg(31.5, G, A, "⑩", "ALLOW", dashed=True)
msg(29.3, A, M, "⑪", "X-PAYMENT（EIP-3009 签名）")
msg(27.1, M, H, "⑫", "验签 + 链上结算（USDC）")
msg(24.9, H, M, "⑬", "结算回执（tx hash）", dashed=True)
msg(22.7, M, A, "⑭", "200 + 数据交付", dashed=True)

# ═══ 阶段三 · 例外分支 ═══
ax.add_patch(FancyBboxPatch((11, 11.5), 87, 7.8, boxstyle="round,pad=0.3",
                            fc="#FFF8F0", ec="#B06000", lw=1.2, zorder=1))
ax.text(12.5, 17.9, "⑮  ASK（超阈值/白名单外）→ Agent 在对话中转述 → 用户回复“同意”"
        "→ human_approved=true 重调 → 回到 ⑩", fontsize=9, color="#333333", zorder=2)
ax.text(12.5, 15.5, "⑯  DENY（超限/注入/断链）→ 支付终止，签名从未发生，原因写入审计",
        fontsize=9, color="#333333", zorder=2)
ax.text(12.5, 13.1, "⑰  预算不足 → Agent 提交预算申请 → 用户批准并签名 v2"
        "（prev_hash 接续）→ 网关 anchor v2 → 新策略即刻生效", fontsize=9,
        color="#333333", zorder=2)

# ═══ 阶段四 · 审计复盘 ═══
msg(8.6, U, G, "⑱", "get_spend_report / get_audit_trail", dashed=True)
msg(6.0, U, H, "⑲", "交叉核对：审计中的策略版本哈希 ⇄ 链上承诺", dashed=True)
selfnote(3.6, G, "", "审计落盘：audit.jsonl（引用策略版本+哈希+裁决理由）", C["audit"] if "audit" in C else "#5F6368")

ax.text(54.5, 70.2, "Agent 支付审批网关 · 整体使用流程", ha="center",
        fontsize=17, weight="bold")

out = Path(__file__).parent.parent / "strategy" / "flow-v1.png"
fig.savefig(out, bbox_inches="tight", dpi=160)
print(f"saved: {out}")
