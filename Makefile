.PHONY: install test cov lint format typecheck check bench demo build clean

VENV := .venv/bin

install:
	uv venv
	uv pip install -e ".[dev]"

test:
	$(VENV)/pytest

cov:
	$(VENV)/coverage run -m pytest -v
	$(VENV)/coverage report

lint:
	$(VENV)/ruff check src tests examples benchmarks
	$(VENV)/ruff format --check src tests examples benchmarks

format:
	$(VENV)/ruff check --fix src tests examples benchmarks
	$(VENV)/ruff format src tests examples benchmarks

typecheck:
	$(VENV)/mypy src

check: lint typecheck cov

bench:
	$(VENV)/python benchmarks/bench_overhead.py

demo:
	$(VENV)/python examples/bookstore_app.py

build:
	uv build

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov .flask_lens.sqlite3*
