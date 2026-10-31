.PHONY: test up down scale eval load-ui dev smoke fault
test:        ; python -m pytest -q
up:          ; test -f .env || cp .env.example .env; docker compose up --build -d
scale:       ; docker compose up -d --scale agent-worker=3
down:        ; docker compose down
smoke:       ; python scripts/submit.py food
fault:       ; scripts/fault_demo.sh
eval:        ; python -m evaluation.run_eval --label mine --mode http
load-ui:     ; docker compose --profile load up locust
dev:         ; scripts/devstack.sh start 2
