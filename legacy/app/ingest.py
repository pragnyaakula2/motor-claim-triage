
from pathlib import Path
import json
from pypdf import PdfReader

PROJECT_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = PROJECT_DIR / "corpus"
OUTPUT_DIR = PROJECT_DIR / "data"
OUTPUT_FILE = OUTPUT_DIR / "extracted_pages.json"


def extract_pdf_text(pdf_path: Path) -> list[dict]:
    """Extract text from a PDF, preserving page numbers."""
    reader = PdfReader(str(pdf_path))
    pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""

        if text.strip():
            pages.append({
                "source": pdf_path.name,
                "page": page_number,
                "text": text.strip(),
            })

    return pages


def main():
    pdf_files = sorted(CORPUS_DIR.glob("*.pdf"))

    if not pdf_files:
        raise FileNotFoundError(
            f"No PDF files found in {CORPUS_DIR}"
        )

    all_pages = []

    for pdf_path in pdf_files:
        pages = extract_pdf_text(pdf_path)
        total_chars = sum(len(page["text"]) for page in pages)

        print(f"\nFile: {pdf_path.name}")
        print(f"Pages extracted: {len(pages)}")
        print(f"Characters extracted: {total_chars}")

        all_pages.extend(pages)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(all_pages, file, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_pages)} pages to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

