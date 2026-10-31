import csv
import json
from collections import defaultdict
from pathlib import Path

INPUT = Path("evaluation/retrieval_results_after_reranking.csv")
OUTPUT = Path("evaluation/relevance_metrics.json")

# Load manually labeled retrieval results.
with INPUT.open(newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

if not rows:
    raise SystemExit("No evaluation results found.")

required = {"claim_id", "rank", "manually_relevant"}
missing = required - set(rows[0].keys())
if missing:
    raise SystemExit(f"Missing CSV columns: {sorted(missing)}")

# Group passages by claim and order them by retrieval rank.
claims = defaultdict(list)

for row in rows:
    label = row["manually_relevant"].strip()
    if label not in {"0", "1"}:
        raise SystemExit("Some relevance labels are missing or invalid.")

    claims[row["claim_id"]].append({
        "rank": int(row["rank"]),
        "relevant": int(label),
    })

for claim_id in claims:
    claims[claim_id].sort(key=lambda x: x["rank"])

results = {}

for k in (3, 5):
    hits = []
    precisions = []

    for passages in claims.values():
        top_k = passages[:k]
        relevant_count = sum(p["relevant"] for p in top_k)

        hits.append(int(relevant_count > 0))
        precisions.append(relevant_count / len(top_k) if top_k else 0)

    results[f"Hit@{k}"] = round(sum(hits) / len(hits), 4)
    if k == 5:
        results["Precision@5"] = round(
            sum(precisions) / len(precisions), 4
        )

reciprocal_ranks = []

for passages in claims.values():
    first_relevant_rank = next(
        (p["rank"] for p in passages if p["relevant"] == 1),
        None,
    )
    reciprocal_ranks.append(
        1 / first_relevant_rank if first_relevant_rank else 0
    )

results["MRR"] = round(
    sum(reciprocal_ranks) / len(reciprocal_ranks), 4
)
results["number_of_claims"] = len(claims)
results["number_of_passages"] = len(rows)
results["manually_relevant_passages"] = sum(
    p["relevant"] for passages in claims.values() for p in passages
)

OUTPUT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

print("Retrieval evaluation metrics")
print("=" * 32)
for name, value in results.items():
    print(f"{name}: {value}")
print(f"\nSaved results to {OUTPUT}")
