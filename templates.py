# -*- coding: utf-8 -*-
"""
templates.py — 策略模板库：进阶用户的"模板+滑块"定制方式

普通用户用自然语言，开发者/企业用 DSL，中间地带用模板：
选模板 → 调参数 → 生成自然语言规则 → 走统一的编译 + 签名发布流程。
"""
from __future__ import annotations

TEMPLATES = {
    "personal_assistant": {
        "name": "个人助理 Agent",
        "desc": "日常小额消费：严格白名单 + 低单笔上限 + 中额转人工",
        "params": {"daily": 50, "per_tx": 10, "ask_above": 5},
        "rule": "每天开销不超过 {daily} 美元，单笔不超过 {per_tx} 美元；"
                "只允许付给白名单服务商（{whitelist}）；超过 {ask_above} 美元要我手机确认",
    },
    "travel_agent": {
        "name": "旅行 Agent",
        "desc": "机酒预订：限定旅行类商户，大额必须人工",
        "params": {"daily": 300, "per_tx": 200, "ask_above": 50},
        "rule": "每天开销不超过 {daily} 美元，单笔不超过 {per_tx} 美元；"
                "只允许付给白名单服务商（{whitelist}）；超过 {ask_above} 美元要我手机确认",
    },
    "dev_api_budget": {
        "name": "开发者 API 消费",
        "desc": "Agent 自主调付费 API：高频小额，免确认阈值可放宽",
        "params": {"daily": 100, "per_tx": 5, "ask_above": 20},
        "rule": "每天 API 开销不超过 {daily} 美元，单笔不超过 {per_tx} 美元；"
                "只允许付给白名单服务商（{whitelist}）；超过 {ask_above} 美元要我手机确认",
    },
    "procurement": {
        "name": "企业采购 Agent",
        "desc": "B2B 采购：高额度 + 极低免确认阈值，几乎笔笔审批",
        "params": {"daily": 5000, "per_tx": 1000, "ask_above": 100},
        "rule": "每天采购开销不超过 {daily} 美元，单笔不超过 {per_tx} 美元；"
                "只允许付给白名单供应商（{whitelist}）；超过 {ask_above} 美元要我审批",
    },
}


def list_templates() -> list[dict]:
    return [{"id": k, "name": v["name"], "desc": v["desc"],
             "params": v["params"]} for k, v in TEMPLATES.items()]


def render_template(template_id: str, whitelist: list[str], **overrides) -> str:
    """模板 + 参数 → 自然语言规则（进入统一编译管线）"""
    t = TEMPLATES[template_id]
    params = {**t["params"], **overrides}
    return t["rule"].format(whitelist=", ".join(whitelist), **params)
