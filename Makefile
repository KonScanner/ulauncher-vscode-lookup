.PHONY: format lint typecheck test

RUFF := uv run --extra dev ruff

format:
	$(RUFF) check --fix .
	$(RUFF) format .

lint:
	$(RUFF) check .
	$(RUFF) format --check .

typecheck:
	env -u VIRTUAL_ENV ty check main.py

test:
	uv run --extra dev pytest -q tests/
