
import json
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = PROJECT_DIR / "data" / "extracted_pages.json"
OUTPUT_FILE = PROJECT_DIR / "data" / "chunks.json"

CHUNK_SIZE = 700
CHUNK_OVERLAP = 100


def split_text(text: str) -> list[str]:
    """Split policy text near sentence or paragraph boundaries."""
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return []

    sentences = re.split(r"(?<=[.!?;])\s+", text)

    chunks = []
    current = ""

    for sentence in sentences:
        if len(sentence) > CHUNK_SIZE:
            if current:
                chunks.append(current.strip())
                current = ""

            start = 0
            while start < len(sentence):
                end = min(start + CHUNK_SIZE, len(sentence))
                piece = sentence[start:end].strip()

                if piece:
                    chunks.append(piece)

                if end == len(sentence):
                    break

                start = end - CHUNK_OVERLAP

            continue

        candidate = f"{current} {sentence}".strip()

        if len(candidate) <= CHUNK_SIZE:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())

            overlap = current[-CHUNK_OVERLAP:] if current else ""
            current = f"{overlap} {sentence}".strip()

            if len(current) > CHUNK_SIZE:
                current = sentence

    if current:
        chunks.append(current.strip())

    return chunks


def main():
    with INPUT_FILE.open("r", encoding="utf-8") as file:
        pages = json.load(file)

    all_chunks = []

    for page in pages:
        page_chunks = split_text(page["text"])

        for index, chunk in enumerate(page_chunks, start=1):
            all_chunks.append({
                "chunk_id": (
                    f'{page["source"]}-p{page["page"]}-c{index}'
                ),
                "source": page["source"],
                "page": page["page"],
                "text": chunk,
            })

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(all_chunks, file, ensure_ascii=False, indent=2)

    print(f"Pages processed: {len(pages)}")
    print(f"Chunks created: {len(all_chunks)}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

