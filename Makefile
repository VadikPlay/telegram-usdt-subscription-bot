.PHONY: help install run test lint selftest up down logs export

help:
	@echo "make install    - install into the current environment (editable, with dev extras)"
	@echo "make run        - run the bot locally"
	@echo "make test       - run the unit tests"
	@echo "make lint       - run ruff"
	@echo "make selftest   - end-to-end dry run with a fake chain and fake Telegram"
	@echo "make up         - docker compose up -d"
	@echo "make down       - docker compose down"
	@echo "make logs       - follow container logs"

install:
	pip install -e ".[dev]"

run:
	python -m gatekit

test:
	pytest -q

lint:
	ruff check src tests scripts

selftest:
	python scripts/selftest.py

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100
