# -*- coding: utf-8 -*-
"""
templates.py — policy template library: the "template + sliders" customization
path for advanced users.

Casual users write natural language; developers/enterprises use a DSL; the
middle ground picks a template → tunes parameters → generates a natural-language
rule → goes through the same compile + sign + publish pipeline.
"""
from __future__ import annotations

TEMPLATES = {
    "personal_assistant": {
        "name": "Personal Assistant Agent",
        "desc": "everyday small purchases: strict whitelist + low per-tx cap + mid-range human approval",
        "params": {"daily": 50, "per_tx": 10, "ask_above": 5},
        "rule": "Daily spend cap {daily} dollars, max {per_tx} dollars per transaction; "
                "only pay whitelisted providers ({whitelist}); "
                "any payment above {ask_above} dollars requires my approval",
    },
    "travel_agent": {
        "name": "Travel Agent",
        "desc": "flights & hotels: travel merchants only, large amounts always need a human",
        "params": {"daily": 300, "per_tx": 200, "ask_above": 50},
        "rule": "Daily spend cap {daily} dollars, max {per_tx} dollars per transaction; "
                "only pay whitelisted providers ({whitelist}); "
                "any payment above {ask_above} dollars requires my approval",
    },
    "dev_api_budget": {
        "name": "Developer API Spend",
        "desc": "agent calls paid APIs autonomously: high-frequency small amounts, relaxed approval threshold",
        "params": {"daily": 100, "per_tx": 5, "ask_above": 20},
        "rule": "Daily API spend cap {daily} dollars, max {per_tx} dollars per transaction; "
                "only pay whitelisted providers ({whitelist}); "
                "any payment above {ask_above} dollars requires my approval",
    },
    "procurement": {
        "name": "Enterprise Procurement Agent",
        "desc": "B2B purchasing: high ceilings + very low auto-approve threshold, nearly every payment reviewed",
        "params": {"daily": 5000, "per_tx": 1000, "ask_above": 100},
        "rule": "Daily procurement spend cap {daily} dollars, max {per_tx} dollars per transaction; "
                "only pay whitelisted suppliers ({whitelist}); "
                "any payment above {ask_above} dollars requires my approval",
    },
}


def list_templates() -> list[dict]:
    return [{"id": k, "name": v["name"], "desc": v["desc"],
             "params": v["params"]} for k, v in TEMPLATES.items()]


def render_template(template_id: str, whitelist: list[str], **overrides) -> str:
    """template + parameters → natural-language rule (enters the shared compile pipeline)"""
    t = TEMPLATES[template_id]
    params = {**t["params"], **overrides}
    return t["rule"].format(whitelist=", ".join(whitelist), **params)
