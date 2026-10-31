FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir locust==2.47.0 httpx==0.28.1 pydantic==2.14.1
COPY claimtriage ./claimtriage
COPY evaluation ./evaluation
COPY loadtest ./loadtest
ENV PYTHONPATH=/app
