# Contributing to flask-lens

Thank you for your interest in contributing to `flask-lens`.

## Development Setup

```bash
git clone https://github.com/duhanguler/flask-lens.git
cd flask-lens
uv venv
uv pip install -e ".[dev]"
```

## Quality Checks

Before opening a pull request, ensure all checks pass:

```bash
make check
```

This runs:
- `ruff check src tests examples benchmarks`
- `ruff format --check src tests examples benchmarks`
- `mypy src`
- `pytest --cov=flask_lens`

## Pull Request Guidelines

- Keep core runtime dependencies minimal (`flask` and `werkzeug` only; ORM and HTTP client integrations must remain optional).
- Add unit and integration tests for all new behavior and bug fixes.
- Ensure all public APIs carry strict PEP 484 type annotations.
- Update documentation and `CHANGELOG.md` for user-facing changes.
