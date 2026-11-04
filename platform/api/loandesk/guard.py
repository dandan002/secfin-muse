"""Prompt-injection defence (spec: *Controls, risk and compliance*). Counterparty text is data, never instructions.

Two layers, both here so the chat parser and the agent share them:

* ``injection()``: the rules' tripwire, run on every counterparty message before any model sees it. It reads the
  text the way a model would: Unicode-normalised (full-width letters, ligatures), invisible characters dropped, and
  letters spelled out with spaces or dots joined up ("I g n o r e"). It looks for what no counterparty needs to say
  to a stock-loan desk: talk to an AI, claims that our own approvals were waived, and attempts to forge the
  conversation or the fence. A hit flags the message; nothing acts on it.
* ``untrusted()``: the fence. Text that gets past the tripwire reaches a model only inside ``<untrusted>`` tags,
  and anything inside that looks like a fence tag, in any case, spacing or width, is removed first, so the text
  can't close the fence early and speak outside it.

Neither is the last line: nothing a model reads becomes an external side effect without a trader approving its
card at the gate. ``ingest/redteam.py`` is the red-team set these are scored against.
"""

from __future__ import annotations

import re
import unicodedata

_INVISIBLE = {"Cf", "Cc"}  # zero-width spaces and joiners, bidi overrides, soft hyphens; control characters
_SPELLED = (re.compile(r"\b(?:[a-z][.\-_*]){2,}[a-z]\b", re.I),  # "i.g.n.o.r.e y.o.u.r": one word each
            re.compile(r"\b(?:[a-z] ){3,}[a-z]\b", re.I))  # "I g n o r e"
_TAG = re.compile(r"<\s*/?\s*untrusted\b[^>]*>", re.I)

_PATTERNS = (
    # telling the desk to drop its rules
    r"\b(ignore|disregard|forget|override|bypass)\b (all |any )?((your|the|previous|prior|desk'?s?|loandesk'?s?|"
    r"these|those|its) )*(own )?(limits?|rules|instructions|checks?|restrictions|guidelines|polic(y|ies)|prompt|"
    r"restricted list|threshold list|(risk|credit) limits?)\b",
    r"\b(ignore|forget|disregard) (everything|all|what('?s| is)?) (above|before|you (were|have been) told)\b",
    r"\bforget everything\b|\byou are now\b|\bfrom now on\b|\bnew instructions\b",
    # talking to a model rather than a trader
    r"\b(system|assistant|developer) (note|prompt|message|instructions?)\b",
    r"(^|\n)\s*(system|assistant|developer)\s*:",
    r"\b(note|message|instructions?) (to|for) (the |our |your )?(loandesk )?(bot|agent|ai|assistant|model|llm)\b",
    r"\b(hey|hi|dear|attn:?) (the )?(loandesk )?(bot|assistant|llm)\b",
    r"(^|[\s(\[])(bot|assistant|llm)\s*[:,]",
    r"\bloandesk[- ](ai|agent|bot|assistant)\b",
    r"\bas (an?|the) (loandesk )?(ai|language model|assistant|bot)\b",
    # claiming our approvals don't apply
    r"\b(skip|bypass|without|no)\b (the |any |your )?(usual )?(approval|review|sign-?off|checks?|queue)\b",
    r"\b(approval|review|sign-?off)s? (is |are )?(not |no longer )?(required|needed|necessary)\b",
    r"\bpre-?(authori[sz]ed|approved|cleared)\b|\bcleared upstream\b",
    r"\b(your|the desk'?s?) (compliance|risk|head trader|management|desk head|supervisor)( team)?\b[^.\n]{0,30}"
    r"\b(said|says|signed|cleared|approved|waived|ok'?d|authori[sz]ed|fine)\b",
    r"\b(compliance|risk)\b[^.\n]{0,20}\b(waived|signed off)\b",
    r"\bno need to (check|review|verify)\b",
    r"\breply ['\"]?confirmed\b",
    r"\bauto-?(book|approve|execute|confirm)\b",
    r"\b(must|have to) (approve|book|confirm|execute)\b",
    r"\bapprove (everything|all|every)\b",
    # smuggling: encoded payloads, hidden markup, forged fences and forged conversation lines
    r"\bdecode (this|it|and)\b",
    r"\[\[.*?\]\]|<!--|<\s*/?\s*(untrusted|system|instructions?)\b",
    r"\btreat (this|it|the next|my next)\b[^.\n]{0,40}\bas (coming )?from\b",
    r"(^|\n)\s*#\d+\b[^\n]{0,20}\b(US|THEM)\s*\(",
    r"(^|\n)\s*US \([\w.\-]+\)\s*:",
)
INJECTION = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.I)


def normalize(text: str) -> str:
    """What a model would read: NFKC (full-width and compatibility forms folded), invisible characters dropped."""
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c) not in _INVISIBLE or c in "\n\t")


def injection(text: str) -> str | None:
    """The phrase that gives the message away as an injection, or None."""
    flat = normalize(text)
    for spelled in _SPELLED:
        flat = spelled.sub(lambda m: re.sub(r"[ .\-_*]", "", m[0]), flat)
    m = INJECTION.search(flat)
    return m[0].strip() if m else None


def untrusted(text: str) -> str:
    """Counterparty text fenced for a model. Fence-like tags inside it are removed, whatever their case or width."""
    return "<untrusted>" + _TAG.sub("", normalize(text)) + "</untrusted>"
