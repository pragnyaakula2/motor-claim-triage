
from app.main import retrieve_claim_evidence


class FakeRetriever:
    def search(self, query, top_k=5):
        if "coverage" in query.lower():
            return [
                {
                    "source": "policy.pdf",
                    "page": 2,
                    "chunk_id": "chunk-1",
                    "score": 0.8,
                    "text": "Coverage clause",
                }
            ]

        return [
            {
                "source": "policy.pdf",
                "page": 2,
                "chunk_id": "chunk-1",
                "score": 0.9,
                "text": "Coverage clause",
            },
            {
                "source": "policy.pdf",
                "page": 3,
                "chunk_id": "chunk-2",
                "score": 0.7,
                "text": "Exclusion clause",
            },
        ]


def test_retrieval_deduplicates_chunks(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "retriever", FakeRetriever())

    evidence = retrieve_claim_evidence(
        "My car was damaged in a collision while driving to work.",
        "Own damage",
    )

    keys = [
        (item["source"], item["page"], item["chunk_id"])
        for item in evidence
    ]

    assert len(keys) == len(set(keys))
    assert len(evidence) == 2


def test_retrieval_tracks_matching_queries(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "retriever", FakeRetriever())

    evidence = retrieve_claim_evidence(
        "My car was damaged in a collision while driving to work.",
        "Own damage",
    )

    shared_chunk = next(
        item for item in evidence
        if item["chunk_id"] == "chunk-1"
    )

    assert len(shared_chunk["retrieval_queries"]) > 1
    assert shared_chunk["score"] == 0.9


def test_retrieval_returns_empty_when_no_matches(monkeypatch):
    import app.main as main

    class EmptyRetriever:
        def search(self, query, top_k=5):
            return []

    monkeypatch.setattr(main, "retriever", EmptyRetriever())

    evidence = retrieve_claim_evidence(
        "My car was damaged in a collision while driving to work.",
        "Own damage",
    )

    assert evidence == []

