# SQL Query Budgets & Pytest Guardrails

Django ships `TestCase.assertNumQueries`, which lets teams lock in SQL query counts in CI before N+1 regressions ever reach production. `flask-lens` brings first-class query-budget and N+1 assertions to Flask and SQLAlchemy — both as standalone context managers and as an auto-registered `pytest` plugin.

## Standalone Context Managers

All helpers accept a SQLAlchemy `Engine`, a `Session`, a `Flask-SQLAlchemy` `db` object, or a `Flask` `app`:

```python
from flask_lens import (
    assert_max_queries,
    assert_no_n_plus_one,
    assert_num_queries,
    capture_queries,
)


def test_authors_endpoint_query_budget(client, db):
    # Exact query count (Django assertNumQueries equivalent)
    with assert_num_queries(db, 2):
        resp = client.get("/api/authors?optimized=1")
        assert resp.status_code == 200

    # Upper-bound budget
    with assert_max_queries(db, 3):
        client.get("/api/authors?optimized=1")

    # Fail automatically if any N+1 query pattern repeats >= 3 times
    with assert_no_n_plus_one(db, threshold=3):
        client.get("/api/authors?optimized=1")
```

When an assertion fails, `QueryAssertionError` prints every captured query, its duration, N+1/duplicate/slow tags, and the exact application call site:

```text
flask_lens.testing.QueryAssertionError: Expected exactly 2 SQL queries, but executed 7.
Captured 7 SQL queries (1.24ms total):
   1. [  0.22ms] SELECT authors.id, authors.name FROM authors ORDER BY authors.id  (views.py:24 in list_authors)
   2. [  0.17ms] [N+1] SELECT books.id, books.title FROM books WHERE ? = books.author_id  (views.py:30 in list_authors)
   ...

Detected N+1 patterns:
  - 6x (1.02ms) at views.py:30 in list_authors: SELECT books.id, books.title FROM books WHERE ? = books.author_id
    Hint: Executed 6 similar SELECT queries against books. Consider eager loading with SQLAlchemy `selectinload(...)` / `joinedload(...)` or batching into a single `IN (...)` query.
```

## Auto-Registered Pytest Plugin

Installing `flask-lens` registers the `flask_lens` `pytest11` plugin automatically.

### Declarative Markers

```python
import pytest


@pytest.mark.max_queries(2)
@pytest.mark.no_n_plus_one
def test_authors_list_is_fast(client, db):
    resp = client.get("/api/authors?optimized=1")
    assert resp.status_code == 200
```

Markers attach only around the test body (`pytest_pyfunc_call`), so database seeding performed inside fixtures never pollutes your test's query budget.

### Built-In Fixtures

- `lens_capture`: factory returning `capture_queries`
- `lens_assert_num_queries`: factory returning `assert_num_queries`
- `lens_assert_max_queries`: factory returning `assert_max_queries`
- `lens_assert_no_n_plus_one`: factory returning `assert_no_n_plus_one`

```python
def test_inspect_captured_queries(client, db, lens_capture):
    with lens_capture(db) as cap:
        client.get("/api/books/search?q=Systems")

    assert cap.count == 1
    assert cap.total_ms < 25.0
    assert not cap.has_n_plus_one
```
