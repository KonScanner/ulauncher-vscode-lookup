.PHONY: ruff typecheck

ruff:
	ruff check .

typecheck:
	env -u VIRTUAL_ENV ty check main.py
