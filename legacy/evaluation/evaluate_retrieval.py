
import csv
import json
from pathlib import Path

from app import main as api
from app.retrieve import PolicyRetriever

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "evaluation"

TEST_CASES = [
    {
        "claim_id": "EVAL-001",
        "description": "My car was damaged in a collision while driving to work.",
        "damage": "Own damage",
    },
    {
        "claim_id": "EVAL-002",
        "description": "My parked car was stolen and has not been recovered.",
        "damage": "Vehicle theft",
    },
    {
        "claim_id": "EVAL-003",
        "description": "Heavy rain caused water damage to my vehicle.",
        "damage": "Water damage",
    },
    {
        "claim_id": "EVAL-004",
        "description": "My private car was damaged while delivering food for payment.",
        "damage": "Own damage during commercial use",
    },
    {
        "claim_id": "EVAL-005",
        "description": "My car was damaged while carrying passengers for payment.",
        "damage": "Own damage during paid passenger transport",
    },
]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load the actual policy index.
    api.retriever = PolicyRetriever()

    all_results = []
    summary = {
        "test_cases": len(TEST_CASES),
        "total_evidence_results": 0,
        "cases_with_evidence": 0,
        "cases_without_evidence": [],
        "results_with_source": 0,
        "results_with_valid_page": 0,
        "results_with_text": 0,
        "limitations": [
            "Retrieval results are not proof of legal relevance.",
            "Similarity scores are not coverage probabilities.",
            "Manual relevance labels are required for Hit@K and MRR.",
            "Results use one policy corpus and five test scenarios.",
        ],
    }

    for case in TEST_CASES:
        evidence = api.retrieve_claim_evidence(
            claim_description=case["description"],
            claimed_damage=case["damage"],
            top_k_per_query=5,
        )

        if evidence:
            summary["cases_with_evidence"] += 1
        else:
            summary["cases_without_evidence"].append(case["claim_id"])

        for rank, item in enumerate(evidence, start=1):
            source_valid = bool(
                isinstance(item.get("source"), str)
                and item["source"].strip()
            )
            page_valid = (
                isinstance(item.get("page"), int)
                and not isinstance(item.get("page"), bool)
                and item["page"] >= 1
            )
            text_valid = bool(
                isinstance(item.get("text"), str)
                and item["text"].strip()
            )

            summary["results_with_source"] += int(source_valid)
            summary["results_with_valid_page"] += int(page_valid)
            summary["results_with_text"] += int(text_valid)

            all_results.append({
                "claim_id": case["claim_id"],
                "claim_description": case["description"],
                "claimed_damage": case["damage"],
                "rank": rank,
                "source": item.get("source", ""),
                "page": item.get("page", ""),
                "chunk_id": item.get("chunk_id", ""),
                "score": item.get("score", ""),
                "retrieval_query_count": len(
                    item.get("retrieval_queries", [])
                ),
                "text": item.get("text", ""),
                "source_valid": source_valid,
                "page_valid": page_valid,
                "text_valid": text_valid,
                "manually_relevant": "",
            })

        print(f"\n{case['claim_id']}: {case['description']}")
        print(f"Unique evidence passages: {len(evidence)}")

        for item in evidence[:5]:
            print(
                f"  Page {item.get('page')} | "
                f"Score {item.get('score')} | "
                f"{item.get('source')}"
            )
            print(
                "  " + item.get("text", "").replace("\n", " ")[:180]
            )

    summary["total_evidence_results"] = len(all_results)

    for key in (
        "results_with_source",
        "results_with_valid_page",
        "results_with_text",
    ):
        summary[key + "_rate"] = (
            round(summary[key] / len(all_results), 4)
            if all_results else 0.0
        )

    csv_path = OUTPUT_DIR / "retrieval_results.csv"
    json_path = OUTPUT_DIR / "retrieval_summary.json"

    if all_results:
        with csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=list(all_results[0].keys()),
            )
            writer.writeheader()
            writer.writerows(all_results)

    with json_path.open("w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, ensure_ascii=False)

    print("\n--- Evaluation Summary ---")
    print(json.dumps(summary, indent=2))
    print(f"\nDetailed results: {csv_path}")
    print(f"Summary metrics: {json_path}")


if __name__ == "__main__":
    main()

