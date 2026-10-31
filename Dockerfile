# One image for api / triage-agent / agent-worker (different commands in compose) + a retrieval target that bakes the index.
FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY claimtriage ./claimtriage
COPY contracts ./contracts
COPY prompts ./prompts
COPY web ./web
COPY stub ./stub
RUN useradd -r -u 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "claimtriage.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM app AS retrieval
USER root
ARG EMBEDDING_BACKEND=hashing
ARG EMBEDDING_MODEL=
ENV EMBEDDING_BACKEND=${EMBEDDING_BACKEND} EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    DATA_DIR=/app/data CORPUS_DIR=/app/corpus HF_HOME=/opt/hf
COPY requirements-embeddings.txt .
# The embedding model is downloaded at BUILD time so containers never need the internet at start-up.
RUN if [ "$EMBEDDING_BACKEND" = "sentence_transformers" ]; then \
      pip install -r requirements-embeddings.txt && \
      python -c "from sentence_transformers import SentenceTransformer as S; import os; S(os.environ['EMBEDDING_MODEL'])"; \
    fi
COPY corpus ./corpus
# Build the index once at image build (clause-aware chunking, embeddings, FAISS, manifest).
RUN python -m claimtriage.pipeline.build && chown -R app /app/data /opt/hf 2>/dev/null || true
ENV HF_HUB_OFFLINE=1
USER app
EXPOSE 8100
CMD ["uvicorn", "claimtriage.retrieval.service:app", "--host", "0.0.0.0", "--port", "8100"]
