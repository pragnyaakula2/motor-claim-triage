
import json
from pathlib import Path

import faiss
from sentence_transformers import SentenceTransformer

PROJECT_DIR = Path(__file__).resolve().parent.parent
INDEX_DIR = PROJECT_DIR / "data" / "faiss_index"

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class PolicyRetriever:
    def __init__(self):
        index_path = INDEX_DIR / "policy.index"
        chunks_path = INDEX_DIR / "chunks.json"

        if not index_path.exists() or not chunks_path.exists():
            raise FileNotFoundError(
                "FAISS index not found. Run: python app/build_index.py"
            )

        self.model = SentenceTransformer(MODEL_NAME)
        self.index = faiss.read_index(str(index_path))

        with chunks_path.open("r", encoding="utf-8") as file:
            self.chunks = json.load(file)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        query_embedding = self.model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

        scores, indices = self.index.search(
            query_embedding.astype("float32"),
            min(top_k, len(self.chunks)),
        )

        results = []

        for score, index in zip(scores[0], indices[0]):
            if index < 0:
                continue

            chunk = self.chunks[int(index)]

            results.append({
                "source": chunk["source"],
                "page": chunk["page"],
                "chunk_id": chunk["chunk_id"],
                "score": round(float(score), 4),
                "text": chunk["text"],
            })

        return results


if __name__ == "__main__":
    retriever = PolicyRetriever()

    queries = [
        "What is the permitted use of a private car under this policy?",
        "What uses of the insured vehicle are excluded or restricted?",
        "Does this policy cover a private car used for food delivery or commercial purposes?",
        "What does the policy say about carrying passengers for hire or reward?",
        "What exclusions apply to own damage claims?",
    ]

    for query in queries:
        print(f"\n{'=' * 80}")
        print(f"QUERY: {query}\n")

        for result in retriever.search(query, top_k=3):
            print(
                f"[{result['source']} | Page {result['page']} | "
                f"Score {result['score']}]"
            )
            print(result["text"][:700])
            print()

