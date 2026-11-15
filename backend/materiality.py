"""Transparent, rule-based announcement triage.

This is KEYWORD MATCHING, not an LLM and not a full materiality judgement. It
labels what an announcement *looks like* from its NSE subject/description text
so a human can scan a digest. Every label carries the rule that produced it.
First matching rule wins, so order matters (specific/demoting rules first).
Anything unmatched is 'low' / 'other' (conservative: never invent importance).
"""
from __future__ import annotations

import re

TIERS = ("high", "medium", "low")

# (rule_name, category, tier, regex)  -- searched in lowercased subject + description
_RULES = [
    ("insolvency_default_fraud", "risk_event", "high",
     r"\b(insolvency|cirp|nclt|default|fraud|forensic audit|search and seizure|"
     r"winding up|liquidation|cyber ?(security )?incident|ransomware|fire at|"
     r"plant shutdown)\b"),
    ("auditor_resignation", "management_change", "high",
     r"\bresignation of (the )?(statutory )?auditor|\bauditors? (has |have )?resign"),
    ("senior_management_change", "management_change", "high",
     r"\b(resignation|cessation|appointment|re-?appointment|change)\b.{0,60}"
     r"\b(chief executive|ceo|managing director|chief financial|cfo|chairman|"
     r"chief operating|coo|whole[- ]time director)\b|"
     r"\b(chief executive|ceo|managing director|chief financial|cfo|chairman|"
     r"chief operating|coo|whole[- ]time director)\b.{0,60}"
     r"\b(resign|cessation|appoint|re-?appoint|steps? down)"),
    ("routine_secretarial_role", "routine", "low",
     r"\b(company secretary|compliance officer)\b"),
    ("routine_admin", "routine", "low",
     r"\b(trading window|newspaper publication|loss of share certificate|duplicate share|"
     r"regulation 74|esop|esos|voting results|scrutini[sz]er)\b"),
    ("regulatory_action", "legal_regulatory", "high",
     r"\b(sebi order|show[- ]cause notice|penalty|adjudication order|"
     r"income tax (search|demand)|gst demand|cci order|enforcement directorate)\b"),
    ("board_meeting_intimation", "board_meeting", "medium",
     r"\b(board meeting intimation|intimation of board meeting|prior intimation)\b"),
    ("financial_results", "results", "high",
     r"\b(financial results?|unaudited results?|audited results?|quarterly results?|"
     r"results for the (quarter|year|half))\b"),
    ("merger_acquisition", "m_and_a", "high",
     r"\b(acquisition|acquire[sd]?|amalgamation|merger|scheme of arrangement|demerger|"
     r"divestment|slump sale|takeover|open offer)\b"),
    ("credit_rating", "rating", "medium",
     r"\b(credit rating|rating (action|upgrade|downgrade|reaffirm)|crisil|icra|care ratings|"
     r"india ratings|downgrade|upgrade)\b"),
    ("fundraising", "fundraising", "medium",
     r"\b(qip|preferential (issue|allotment)|rights issue|fund ?raising|"
     r"non-convertible debentures?|ncds?|private placement|warrants?)\b"),
    ("shareholder_returns", "corporate_action", "medium",
     r"\b(dividend|buy-?back|bonus (issue|shares)|stock split|sub-?division)\b"),
    ("order_or_contract", "order_win", "medium",
     r"\b(bagging|receipt of order|order win|letter of award|awarded|new order|contract (win|award))\b"),
    ("capacity_or_project", "capex", "medium",
     r"\b(capacity expansion|capex|new plant|commissioning|commercial production)\b"),
    ("board_meeting_outcome", "board_meeting", "medium",
     r"\b(outcome of board meeting|board meeting)\b"),
    ("director_change", "management_change", "medium",
     r"\b(appointment|resignation|cessation|re-?appointment) of .{0,40}\b(director|key managerial|"
     r"senior management)\b"),
    ("investor_call", "investor_call", "medium",
     r"\b(analyst|institutional investor|earnings call|conference call|con\.? call|"
     r"investor (meet|presentation)|transcript|audio recording)\b"),
]
_COMPILED = [(n, c, t, re.compile(rx, re.IGNORECASE | re.DOTALL)) for n, c, t, rx in _RULES]


def classify(subject, description=None) -> dict:
    """Return {'category', 'materiality', 'matched_rule'}. Never raises on odd input."""
    text = " ".join(x for x in (subject, description) if isinstance(x, str)).lower()
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        for name, category, tier, rx in _COMPILED:
            if rx.search(text):
                return {"category": category, "materiality": tier, "matched_rule": name}
    return {"category": "other", "materiality": "low", "matched_rule": "default_unmatched"}
