# -*- coding: utf-8 -*-
"""
policy.py — 意图编译 + 策略评估引擎（原型）

两层结构：
  1. compile_policy(nl_rule)   —— 自然语言规则 → 结构化 Policy
     ⚠️ 原型用模板/正则解析代替；生产中这一层应调用 LLM 做"意图 → 策略"编译，
        这是传统风控决策引擎里不存在的新层。
  2. evaluate(policy, intent, ctx, ledger) —— 五段式评估管线
     顺序即信任层级：语义层（新）→ 名单 → 限额 → 分级审批 → 预算
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------- Policy

@dataclass
class Policy:
    raw_mandate: str                       # 用户原始自然语言指令（语义基准）
    per_tx_max: float = 10.0               # 单笔上限（美元）
    daily_budget: float = 50.0             # 日预算
    allowed_recipients: list = field(default_factory=list)  # 收款方白名单
    require_approval_above: float = 5.0    # 超过此金额转人工
    max_amount_jump_ratio: float = 3.0     # 语义层：金额较预期跳变倍数红线


# ---------------------------------------------------------------- 意图编译

_NUM = r"(\d+(?:\.\d+)?)"

def compile_policy(nl_rule: str) -> Policy:
    """把用户的自然语言规则编译成结构化策略。

    原型：模板解析，覆盖演示所需句式。
    生产：此函数整体替换为 LLM 编译 + 用户确认回环（"我理解为你要求…对吗？"）。
    """
    p = Policy(raw_mandate=nl_rule)

    m = re.search(r"单笔[^0-9]{0,6}" + _NUM, nl_rule)
    if not m:   # English: "max $10 per transaction" / "$10 per payment"
        m = re.search(r"(?:max(?:imum)?\s*)?\$?" + _NUM +
                      r"\s*(?:USD\s*)?per\s+(?:transaction|payment|tx)", nl_rule, re.I)
    if m:
        p.per_tx_max = float(m.group(1))

    m = re.search(r"(?:每天|每日|日)[^0-9]{0,20}" + _NUM, nl_rule)
    if not m:   # English: "Daily spend cap $50" / "$50 per day"
        m = re.search(r"(?:daily|per\s+day|a\s+day)[^0-9$]{0,25}\$?" + _NUM, nl_rule, re.I)
    if m:
        p.daily_budget = float(m.group(1))

    m = re.search(r"超过[^0-9]{0,4}" + _NUM + r"[^0-9]{0,8}(?:确认|批准|审批)", nl_rule)
    if not m:   # English: "any payment above $5 requires my approval"
        m = re.search(r"(?:above|over|exceeding?)\s*\$?" + _NUM +
                      r"[^0-9]{0,25}(?:approval|confirm)", nl_rule, re.I)
    if m:
        p.require_approval_above = float(m.group(1))

    m = re.search(r"[（(]([^（）()]+)[）)]", nl_rule)
    low = nl_rule.lower()
    if m and ("白名单" in nl_rule or "只允许" in nl_rule or "只能" in nl_rule
              or "whitelist" in low or "allowlist" in low or "only pay" in low):
        p.allowed_recipients = [x.strip() for x in re.split(r"[,，、]", m.group(1)) if x.strip()]

    return p


# ---------------------------------------------------------------- 评估上下文

@dataclass
class PaymentIntent:
    """Agent 提交给网关的支付意图（x402 付款载荷的抽象）"""
    amount: float
    recipient: str
    purpose: str                      # Agent 自述的付款目的
    expected_amount: Optional[float] = None   #  mandate 下达时的预期金额（语义层锚点）
    source_content: str = ""          # 触发本次付款的商户返回内容（注入检测对象）


@dataclass
class Decision:
    action: str          # ALLOW / ASK / DENY
    reasons: list = field(default_factory=list)
    layer: str = ""      # 命中的评估层：semantic / whitelist / limit / approval / budget / pass


# ---------------------------------------------------------------- 语义层（新风险类别）

_INJECTION_PATTERNS = [
    r"ignore (all |previous )?instructions",
    r"忽略(之前|以上|所有)的?(指令|规则|指示)",
    r"SYSTEM\s*:", r"<\s*system\s*>",
    r"改为?支付", r"转账(给|至)", r"send (the )?(funds|payment|max)",
]

def semantic_check(policy: Policy, intent: PaymentIntent) -> Optional[Decision]:
    """传统风控里不存在的评估：Agent 的最终意图是否还落在用户原始授权的语义边界内。

    检测三类漂移：
      a. 触发内容中含注入特征（prompt injection 直接证据）
      b. 收款方相对 mandate 上下文被改写（本原型用：来源内容中出现"新收款方"指令信号）
      c. 金额相对 mandate 预期发生异常跳变
    """
    hits = [pat for pat in _INJECTION_PATTERNS
            if re.search(pat, intent.source_content, re.IGNORECASE)]
    if hits:
        return Decision("DENY", layer="semantic",
                        reasons=[f"触发内容含注入特征（{len(hits)} 处）：疑似 prompt injection，"
                                 f"Agent 意图可能已被外部内容劫持"])
    if (intent.expected_amount is not None and intent.expected_amount > 0
            and intent.amount > intent.expected_amount * policy.max_amount_jump_ratio):
        return Decision("DENY", layer="semantic",
                        reasons=[f"金额 ${intent.amount} 超出授权预期 ${intent.expected_amount} 的 "
                                 f"{policy.max_amount_jump_ratio} 倍：意图漂移，与原始授权语义不符"])
    return None


# ---------------------------------------------------------------- 五段式评估管线

def evaluate(policy: Policy, intent: PaymentIntent, spent_today: float) -> Decision:
    # 第 1 层：语义评估（Agent 时代的新风险——意图漂移 / 注入）
    d = semantic_check(policy, intent)
    if d:
        return d

    # 第 2 层：收款方白名单（未知商户不直接拒，转人工）
    if policy.allowed_recipients and intent.recipient not in policy.allowed_recipients:
        return Decision("ASK", layer="whitelist",
                        reasons=[f"收款方 {intent.recipient} 不在白名单内，需人工确认"])

    # 第 3 层：单笔限额
    if intent.amount > policy.per_tx_max:
        return Decision("DENY", layer="limit",
                        reasons=[f"单笔 ${intent.amount} 超过单笔上限 ${policy.per_tx_max}"])

    # 第 4 层：日预算
    if spent_today + intent.amount > policy.daily_budget:
        return Decision("DENY", layer="budget",
                        reasons=[f"本笔将使用今日支出达 ${spent_today + intent.amount:.2f}，"
                                 f"超过日预算 ${policy.daily_budget}"])

    # 第 5 层：分级审批（规则内但高于阈值 → 转人工）
    if intent.amount > policy.require_approval_above:
        return Decision("ASK", layer="approval",
                        reasons=[f"金额 ${intent.amount} 超过免确认阈值 "
                                 f"${policy.require_approval_above}，需人工确认"])

    return Decision("ALLOW", layer="pass", reasons=["规则内，自动放行"])


# ---------------------------------------------------------------- 平台红线（第 0 层）

# 内置保底，任何用户/Agent 策略都不能放宽；网关运营方也不可在运行时关闭
PLATFORM_PER_TX_CEILING = 1000.0     # 单笔绝对上限
PLATFORM_DAILY_CEILING = 5000.0      # 日累计绝对上限


def platform_check(intent: PaymentIntent, spent_today: float) -> Optional[Decision]:
    """第 0 层：平台红线。先于一切用户策略执行。

    内容：注入特征一票否决 + 平台绝对限额。
    存在的意义：即使用户策略被错误配置（或未来编译器出 bug 编译出宽松策略），
    损失也有硬顶。
    """
    hits = [pat for pat in _INJECTION_PATTERNS
            if re.search(pat, intent.source_content, re.IGNORECASE)]
    if hits:
        return Decision("DENY", layer="platform",
                        reasons=[f"平台红线：触发内容含注入特征（{len(hits)} 处），一票否决"])
    if intent.amount > PLATFORM_PER_TX_CEILING:
        return Decision("DENY", layer="platform",
                        reasons=[f"平台红线：单笔 ${intent.amount} 超过绝对上限 "
                                 f"${PLATFORM_PER_TX_CEILING}"])
    if spent_today + intent.amount > PLATFORM_DAILY_CEILING:
        return Decision("DENY", layer="platform",
                        reasons=[f"平台红线：日累计将超过绝对上限 ${PLATFORM_DAILY_CEILING}"])
    return None
