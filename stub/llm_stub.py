"""LLM stub service (D6) - an OpenAI-compatible /v1/chat/completions endpoint with a *scripted* policy.

It is NOT a language model. It returns deterministic responses derived from the conversation state
(the same input always yields the same output) after a configurable, realistic delay, so that:
  * load tests measure this system, not a provider's rate limits (D6),
  * development and CI run offline, and
  * the demo can inject faults on demand (outage, slowness, fabricated citation) - see /admin/fault.

Its answers are computed from the passages the agent actually retrieved, so retrieval quality
still determines answer quality. Do not report its output as model-quality results.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

LAT_MS = int(os.environ.get("STUB_LATENCY_MS", "800"))
JIT_MS = int(os.environ.get("STUB_JITTER_MS", "200"))
app = FastAPI(title="LLM stub")
FAULT = {"mode": "none", "latency_ms": None, "remaining": None}
REQS = Counter("stub_requests_total", "stub requests", ["agent", "outcome"])
LAT = Histogram("stub_request_seconds", "stub latency", ["agent"], buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10))

# ---------------------------------------------------------------- helpers
def snippet(text: str, pattern: str, before: int = 70, after: int = 70) -> str | None:
    """Verbatim excerpt around a regex match, snapped to word boundaries (so quotes stay exact substrings)."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    a = max(0, m.start() - before)
    if a > 0:
        sp = text.find(" ", a)
        a = sp + 1 if 0 <= sp < m.start() else a
    b = min(len(text), m.end() + after)
    if b < len(text):
        sp = text.find(" ", b)
        b = sp if sp != -1 and sp - b < 25 else b
    return text[a:b].strip()


def parse_conv(messages: list[dict]) -> dict:
    first = next((m["content"] for m in messages if m["role"] == "user"), "")
    def tag(name):
        m = re.search(rf"<{name}>(.*?)</{name}>", first, re.S)
        return json.loads(m.group(1)) if m else {}
    claim, ho = tag("claim_json"), tag("handoff_json")
    strat = int((re.search(r"STRATEGY (\d)", first) or [0, 1])[1])
    calls, results = [], []
    for m in messages:
        if m["role"] == "assistant":
            try:
                j = json.loads(m["content"])
                if j.get("action") == "tool":
                    calls.append(j)
            except Exception:
                pass
        elif m["role"] == "user" and m["content"].startswith("TOOL_RESULT"):
            head, _, body = m["content"].partition("\n")
            name = re.search(r"name=(\S+)", head).group(1)
            try:
                results.append((name, json.loads(body)))
            except Exception:
                results.append((name, {}))
    budget = any(m["role"] == "user" and m["content"].startswith("BUDGET_REACHED") for m in messages)
    return {"claim": claim, "ho": ho, "strategy": strat, "calls": calls, "results": results, "budget": budget,
            "feedback": "VERIFICATION_FAILED" in first}


COVER_Q = {
    "own_damage": "indemnify the insured against loss or damage to the vehicle by accidental external means",
    "theft": "loss or damage to the vehicle by burglary housebreaking or theft",
    "third_party": "liability to third parties death of or bodily injury to any person damage to property",
    "total_loss": "total loss constructive total loss insured declared value IDV of the vehicle",
}
COMMERCIAL = ("commercial_delivery", "hire_or_reward")


def passages_of(conv) -> dict[str, dict]:
    out = {}
    for name, data in conv["results"]:
        for p in data.get("passages", []) if isinstance(data, dict) else []:
            out.setdefault(p["clause_id"], p)
    return out


def coverage_turn(msgs: list[dict]) -> dict:
    c = parse_conv(msgs)
    if "[PROCEDURE:single_pass]" in (msgs[0]["content"] if msgs else ""):
        # baseline prompt v0: one search with the claim's own words, then answer (what a plain RAG pipeline does)
        if not c["calls"]:
            q = f'{c["claim"].get("claim_description", "")} {c["claim"].get("claimed_damage", "")}'.strip()[:380]
            return {"action": "tool", "tool": "search_policy", "thought": "Search the policy once with the claim wording.", "args": {"query": q, "top_k": 5}}
        return final_answer(c, passages_of(c))
    ho, claim = c["ho"], c["claim"]
    ctype = ho.get("claim_type", "own_damage")
    ent = ho.get("entities", {})
    mode = "keyword" if c["strategy"] >= 2 else "hybrid"
    tools_used = [x["tool"] for x in c["calls"]]
    searches = [x for x in c["calls"] if x["tool"] == "search_policy"]
    pas = passages_of(c)

    if not c["budget"]:
        if not searches:
            # total-loss rules live in definition/condition sections, so do not restrict the first search to 'cover'
            args = {"query": COVER_Q[ctype], "top_k": 5 if ctype == "total_loss" else 4, "mode": mode}
            if ctype != "total_loss":
                args["section_type"] = "cover"
            return {"action": "tool", "tool": "search_policy", "thought": f"Find the grant of cover for a {ctype} claim.", "args": args}
        if "list_cross_references" not in tools_used:
            first = next((r for n, r in c["results"] if n == "search_policy"), {"passages": []})["passages"]
            cover = next((p for p in first if "cover" in p["section_type"]), first[0] if first else None)
            if cover is None:
                return final_answer(c, pas)
            return {"action": "tool", "tool": "list_cross_references", "args": {"clause_id": cover["clause_id"]},
                    "thought": "Check which provisions modify this cover clause."}
        if not any(x["args"].get("section_type") == "exclusion" for x in searches):
            xr = next((r for n, r in c["results"] if n == "list_cross_references"), {"refs": []})
            phrases = [r["phrase"] for r in xr.get("refs", []) if "implicit" not in r["phrase"]][:3]
            use_words = "commercial use delivery hire or reward paid" if ent.get("use_purpose") in COMMERCIAL else "use of the vehicle"
            extra = " under the influence of intoxicating liquor or drugs" if ent.get("intoxication_mentioned") else ""
            q = f"{' '.join(phrases)} {use_words} vehicle used otherwise than in accordance with{extra}"
            return {"action": "tool", "tool": "search_policy", "thought": "Search for restrictions that modify the cover, derived from the cross-references and the claim's use.",
                    "args": {"query": q[:380], "section_type": "exclusion", "top_k": 4, "mode": mode}}
        if ctype in ("theft", "total_loss", "third_party") and not any(x["args"].get("section_type") == "condition" for x in searches):
            q = {"theft": "in case of theft immediate notice to the police conditions", "total_loss": "total loss constructive total loss liability shall not exceed insured declared value",
                 "third_party": "conditions notice of claim insured shall give all information assistance"}[ctype]
            return {"action": "tool", "tool": "search_policy", "thought": "Check conditions that bear on this claim type.",
                    "args": {"query": q, "section_type": "condition", "top_k": 3, "mode": mode}}
        if c["strategy"] >= 2 and "get_clause" not in tools_used and pas:
            cid = sorted(pas)[0]
            return {"action": "tool", "tool": "get_clause", "args": {"clause_id": cid}, "thought": "Re-read the clause I intend to cite."}
    return final_answer(c, pas)


def final_answer(c: dict, pas: dict) -> dict:
    ho, claim = c["ho"], c["claim"]
    ctype = ho.get("claim_type", "own_damage")
    ent = ho.get("entities", {})
    texts = list(pas.values())
    cover_pat = {"own_damage": r"by accidental external means", "theft": r"burglary housebreaking or theft",
                 "third_party": r"liability to third parties|death of or bodily injury to any person",
                 "total_loss": r"total loss|constructive total loss"}[ctype]
    cover = next((p for p in texts if "cover" in p["section_type"] and re.search(cover_pat, p["text"], re.I)), None)
    if ctype == "total_loss":
        cover = cover or next((p for p in texts if re.search(r"constructive total loss", p["text"], re.I)), None)
        sec1 = next((p for p in texts if re.search(r"by accidental external means|by fire explosion", p["text"], re.I)), None)
    maxscore = max([p["score"] for p in texts], default=0)
    if cover is None or maxscore < 0.2:
        return {"action": "final", "answer": {
            "position": "INSUFFICIENT_EVIDENCE", "summary": "The retrieved passages do not contain a provision that clearly governs this claim.",
            "within_permitted_use": "not_applicable", "restriction_assessment": [], "citations": [], "confidence": "low",
            "needs_escalation": True, "escalation_reason": "Retrieval returned no clearly applicable cover provision for this claim.",
            "conflicting_clauses": []}}
    cits, assess = [], []
    q = snippet(cover["text"], cover_pat, 35, 40)
    cits.append({"clause_id": cover["clause_id"], "role": "cover", "quote": q})
    restriction = next((p for p in texts if re.search(r"otherwise than in accordance with", p["text"], re.I)), None)
    position, use_flag, summary_bits = "PRIMA_FACIE_WITHIN_COVER", "not_applicable", []
    sec_cover = {"own_damage": "Section I", "theft": "Section I", "third_party": "Section II", "total_loss": "Section I"}[ctype]
    summary_bits.append(f"{cover['section']} (p.{cover['page_start']}) grants cover of the kind described.")
    if restriction:
        rq = snippet(restriction["text"], r"being used otherwise than in accordance with[^;.]{0,60}", 30, 80)
        cits.append({"clause_id": restriction["clause_id"], "role": "restriction", "quote": rq})
        defined = any(re.search(r"social,? domestic", p["text"], re.I) for p in texts)
        if ent.get("use_purpose") in COMMERCIAL:
            if defined:
                position, use_flag = "RESTRICTION_LIKELY_APPLIES", "no"
                reason = "The claim describes paid commercial use, which the retrieved Limitations as to Use text does not permit."
            else:
                position, use_flag = "RESTRICTION_REQUIRES_VERIFICATION", "unverifiable"
                reason = ("The claim describes paid delivery/hire use. General Exception 3(a) removes cover while the vehicle is used otherwise than "
                          "in accordance with the 'Limitations as to Use', but that text sits in the policy Schedule, which is not in the indexed corpus, "
                          "so permitted use cannot be confirmed from the corpus; paid delivery is a potential breach.")
            summary_bits.append(f"{restriction['section']} (p.{restriction['page_start']}) excludes loss while the vehicle is used otherwise than in accordance with the Limitations as to Use. " + reason)
            assess.append({"clause_id": restriction["clause_id"], "effect": "excludes", "applies": "unverified" if not defined else "yes", "reason": reason})
        else:
            reason = ("The narrative describes ordinary use and nothing suggests the vehicle was used otherwise than permitted; the Limitations as to Use "
                      "text itself is in the Schedule, which is not in the corpus.")
            assess.append({"clause_id": restriction["clause_id"], "effect": "limits", "applies": "no", "reason": reason})
            summary_bits.append(f"Checked {restriction['section']} (p.{restriction['page_start']}): no indication of use outside the permitted purposes.")
    if ent.get("intoxication_mentioned"):
        intox = next((p for p in texts if re.search(r"under the influence of intoxicating liquor", p["text"], re.I)), None)
        if intox:
            cits.append({"clause_id": intox["clause_id"], "role": "exclusion", "quote": snippet(intox["text"], r"under the influence of intoxicating liquor or drugs", 80, 20)})
            assess.append({"clause_id": intox["clause_id"], "effect": "excludes", "applies": "unverified",
                           "reason": "The narrative mentions alcohol; the wording excludes loss while the driver is under the influence of intoxicating liquor or drugs."})
            position = "RESTRICTION_LIKELY_APPLIES" if position == "PRIMA_FACIE_WITHIN_COVER" else position
            summary_bits.append(f"{intox['section']} (p.{intox['page_start']}) excludes loss while the driver is under the influence of intoxicating liquor or drugs.")
    if ctype == "theft":
        cond = next((p for p in texts if re.search(r"in case of theft or criminal act", p["text"], re.I)), None)
        if cond:
            cits.append({"clause_id": cond["clause_id"], "role": "condition", "quote": snippet(cond["text"], r"In case of theft or criminal act[^.]{0,120}", 5, 40)})
            filed = claim.get("police_report_filed")
            assess.append({"clause_id": cond["clause_id"], "effect": "conditions", "applies": "no" if filed else "yes",
                           "reason": "Immediate notice to the police is required for theft." + ("" if filed else " No police report is recorded on the claim.")})
            if not filed and position == "PRIMA_FACIE_WITHIN_COVER":
                position = "CONDITION_NOT_MET_RISK"
            summary_bits.append(f"{cond['section']} (p.{cond['page_start']}) requires immediate notice to the police in case of theft.")
    if ctype == "total_loss":
        ctl = next((p for p in texts if re.search(r"exceeds 75% of the IDV", p["text"], re.I)), None)
        if ctl:
            cits.append({"clause_id": ctl["clause_id"], "role": "definition", "quote": snippet(ctl["text"], r"treated as CTL if[^.]{0,140}", 5, 10)})
            summary_bits.append(f"{ctl['section']} (p.{ctl['page_start']}) treats the vehicle as a constructive total loss when retrieval and/or repair cost exceeds 75% of the IDV.")
    if ctype == "third_party":
        sii = cover
        if ent.get("use_purpose") in COMMERCIAL:
            summary_bits.append("Third-party liability cover also excludes occupants carried for hire or reward.")
    summary = " ".join(summary_bits)[:700]
    ans = {"position": position, "summary": summary, "within_permitted_use": use_flag,
           "restriction_assessment": assess, "citations": cits, "confidence": "medium",
           "needs_escalation": False, "escalation_reason": "", "conflicting_clauses": []}
    # ---- injected faults (demo) --------------------------------------------------------------------------
    mode = FAULT["mode"]
    if mode in ("hallucinate_once", "hallucinate_always") and not (mode == "hallucinate_once" and c["feedback"]):
        ans["citations"].append({"clause_id": "PCPP:GE:99", "role": "restriction",
                                 "quote": "The Company shall not be liable for any loss while the vehicle is used for food delivery"})
        ans["summary"] += " The wording expressly names food delivery as an excluded use."
    return {"action": "final", "answer": ans}


def triage_turn(messages: list[dict]) -> dict:
    first = next((m["content"] for m in messages if m["role"] == "user"), "")
    m = re.search(r"<claim_json>(.*?)</claim_json>", first, re.S)
    claim = json.loads(m.group(1)) if m else {}
    text = f'{claim.get("claim_description", "")} {claim.get("claimed_damage", "")}'
    low = text.lower()
    declared = claim.get("claim_type_declared", "unknown")
    if re.search(r"stolen|theft|robbed|snatched|carjack|missing from", low):
        ct = "theft"
    elif re.search(r"total loss|totalled|totaled|write[- ]?off|burnt (down|out)|beyond repair|submerged|washed away", low):
        ct = "total_loss"
    elif claim.get("third_party_involved") and re.search(r"third[- ]party|pedestrian|cyclist|\b(i|we) (hit|rammed|struck|knocked)|their (car|vehicle|property)", low):
        ct = "third_party"
    else:
        ct = "own_damage"
    if re.search(r"deliver|zomato|swiggy|dunzo|blinkit|zepto|courier|parcel|porter", low):
        purpose = "commercial_delivery"
    elif re.search(r"\b(uber|ola|rapido|taxi|cab|ride[- ]?shar|paid passenger|for hire|fare)\b", low):
        purpose = "hire_or_reward"
    elif re.search(r"client|business", low):
        purpose = "business"
    elif re.search(r"commut|going to (work|office)|grocer|shopping|family|personal|weekend", low):
        purpose = "private"
    else:
        purpose = "unknown"
    return {"claim_type": ct, "confidence": 0.82,
            "entities": {"use_purpose": purpose, "paid_activity_mentioned": purpose in ("commercial_delivery", "hire_or_reward") or bool(re.search(r"\bpaid|earn|per trip|per order\b", low)),
                         "intoxication_mentioned": bool(re.search(r"drunk|alcohol|intoxicat|under the influence|few drinks|liquor|beer|whisk", low)),
                         "third_party_injury": bool(claim.get("injuries_reported")), "driver_is_owner": bool(claim.get("driver_is_owner", True)),
                         "damage_summary": str(claim.get("claimed_damage", ""))[:160]},
            "inconsistencies": [], "suspected_injection": bool(re.search(r"ignore (all |previous )?instructions|\"action\"", low))}


@app.post("/admin/fault")
async def set_fault(req: Request):
    body = await req.json()
    FAULT.update({"mode": body.get("mode", "none"), "latency_ms": body.get("latency_ms"), "remaining": body.get("remaining")})
    return FAULT


@app.get("/admin/fault")
def get_fault():
    return FAULT


@app.get("/health")
def health():
    return {"status": "ok", "fault": FAULT["mode"], "latency_ms": LAT_MS}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    agent = "coverage" if "[AGENT:coverage]" in system else "triage" if "[AGENT:triage]" in system else "judge" if "[AGENT:judge]" in system else "other"
    t0 = time.time()
    mode = FAULT["mode"]
    if mode == "down":
        REQS.labels(agent, "503").inc()
        return JSONResponse({"error": "stub: provider down (fault injected)"}, status_code=503)
    if mode == "rate_limited":
        REQS.labels(agent, "429").inc()
        return JSONResponse({"error": "stub: rate limited (fault injected)"}, status_code=429)
    delay = (FAULT["latency_ms"] if (mode == "slow" and FAULT["latency_ms"]) else LAT_MS) + random.uniform(-JIT_MS, JIT_MS)
    await asyncio.sleep(max(0, delay) / 1000)
    if mode == "garbage":
        text = "I'm sorry, I cannot help with that. <html>"
    elif agent == "coverage":
        text = json.dumps(coverage_turn(msgs))
    elif agent == "triage":
        text = json.dumps(triage_turn(msgs))
    elif agent == "judge":
        text = json.dumps({"supported": True, "reason": "stub"})
    else:
        text = "{}"
    REQS.labels(agent, "ok").inc()
    LAT.labels(agent).observe(time.time() - t0)
    pt = sum(est_tokens(m["content"]) for m in msgs)
    return {"id": "stub", "object": "chat.completion", "model": "stub-scripted",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": est_tokens(text), "total_tokens": pt + est_tokens(text)}}
