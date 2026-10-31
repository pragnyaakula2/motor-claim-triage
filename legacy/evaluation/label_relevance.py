
import csv
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Optionally accept a CSV filename as a command-line argument.
filename = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "evaluation/retrieval_results.csv"
)
CSV_FILE = (PROJECT_DIR / filename).resolve()

if not CSV_FILE.is_relative_to(PROJECT_DIR):
    raise SystemExit("Error: CSV must be inside the project directory.")

if not CSV_FILE.exists():
    raise SystemExit(f"Error: File not found: {CSV_FILE}")

with CSV_FILE.open("r", newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)
    rows = list(reader)
    fieldnames = reader.fieldnames

if not rows or not fieldnames:
    raise SystemExit("Error: CSV is empty or has no header.")

if "manually_relevant" not in fieldnames:
    raise SystemExit("Error: CSV has no manually_relevant column.")

print(f"Labeling file: {CSV_FILE}")
print("Relevant = 1, irrelevant = 0, s = skip, q = save and quit.")

for row in rows:
    if row.get("manually_relevant", "").strip() in {"0", "1"}:
        continue

    print("=" * 70)
    print(f"Claim: {row.get('claim_id', '')}")
    print(f"Rank: {row.get('rank', '')} | Page: {row.get('page', '')}")
    print(f"Score: {row.get('score', '')}")
    print(row.get("text", "")[:500])

    while True:
        answer = input("Relevant? (1/0/s/q): ").strip().lower()

        if answer == "q":
            break
        if answer == "s":
            break
        if answer in {"0", "1"}:
            row["manually_relevant"] = answer
            break

        print("Enter 1, 0, s, or q.")

    if answer == "q":
        break

# Save every label entered, including when quitting early.
with CSV_FILE.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"Saved labels to {CSV_FILE}")

