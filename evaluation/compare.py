"""Print a before/after table from two result files:  python evaluation/compare.py before after"""
import json, sys
from pathlib import Path
R = Path(__file__).parent / "results"
a, b = (json.loads((R / f"{n}.json").read_text()) for n in sys.argv[1:3])
rows = [("groundedness", "groundedness (returned answers)"), ("context_relevance", "context relevance (precision of retrieved set)"),
        ("context_recall", "context recall (gold passages found)"), ("faithfulness", "faithfulness (returned answers)"),
        ("hallucination_rate_returned", "hallucination rate (returned answers)"), ("draft_hallucination_rate", "hallucination rate (first draft, pre-verification)"),
        ("draft_groundedness", "groundedness (first draft)"), ("draft_faithfulness", "faithfulness (first draft)"),
        ("task_success_rate", "task success (24 scenarios)"), ("answer_rate_on_answerable", "answered when answerable"), ("position_accuracy", "position accuracy"),
        ("route_accuracy", "route accuracy"), ("section_4_1_correct", "section 4.1 case correct"), ("expected_citation_hit_rate", "expected-citation hit rate"),
        ("first_draft_rejected_by_verifier", "first drafts rejected by verifier"), ("mean_llm_calls", "mean LLM calls / run"), ("mean_tool_calls", "mean tool calls / run")]
print(f'| metric | {sys.argv[1]} ({a["meta"]["prompt_coverage"]}) | {sys.argv[2]} ({b["meta"]["prompt_coverage"]}) | delta |\n|---|---|---|---|')
for k, label in rows:
    x, y = a["aggregate"].get(k), b["aggregate"].get(k)
    d = "" if x is None or y is None else f"{y - x:+.3f}"
    print(f"| {label} | {x} | {y} | {d} |")
