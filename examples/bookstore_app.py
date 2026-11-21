"""Realistic Flask + SQLAlchemy 2.0 Bookstore demo showcasing all flask-lens features.

Run with:
    python examples/bookstore_app.py

Then open:
    http://127.0.0.1:5050/          -> Scenario launcher
    http://127.0.0.1:5050/__lens    -> flask-lens DevTools console
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from flask import Flask, Response, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from flask_lens import Lens


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
lens = Lens()
logger = logging.getLogger("bookstore")


class Author(db.Model):  # type: ignore[name-defined]
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(80), nullable=False)
    books: Mapped[list[Book]] = relationship("Book", back_populates="author")


class Book(db.Model):  # type: ignore[name-defined]
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"), nullable=False, index=True)
    author: Mapped[Author] = relationship("Author", back_populates="books")


class Order(db.Model):  # type: ignore[name-defined]
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)


def seed_database() -> None:
    if db.session.scalar(select(Author.id).limit(1)) is not None:
        return

    authors_data = [
        ("Martin Kleppmann", "UK"),
        ("Alex Petrov", "Germany"),
        ("Brendan Gregg", "USA"),
        ("Luciano Ramalho", "Brazil"),
        ("Armin Ronacher", "Austria"),
        ("Julia Evans", "Canada"),
        ("Tanya Reilly", "Ireland"),
        ("Will Larson", "USA"),
        ("Camille Fournier", "USA"),
        ("Charity Majors", "USA"),
        ("Hillel Wayne", "USA"),
        ("Gergely Orosz", "Netherlands"),
    ]
    categories = ["Distributed Systems", "Databases", "Performance", "Python", "Architecture"]

    for idx, (name, country) in enumerate(authors_data, start=1):
        author = Author(name=name, country=country)
        db.session.add(author)
        db.session.flush()
        for b_num in range(1, 4):
            book = Book(
                title=f"{categories[(idx + b_num) % len(categories)]} Patterns Vol. {b_num} ({name.split()[0]})",
                category=categories[(idx + b_num) % len(categories)],
                price_cents=2900 + (idx * 150) + (b_num * 300),
                author_id=author.id,
            )
            db.session.add(book)
    db.session.commit()


def _compute_book_weight(price_cents: int, salt: int) -> float:
    acc = 0.0
    for i in range(1, 350):
        acc += math.sqrt(price_cents + i * salt) / (i + 1)
    return round(acc, 4)


def _rank_catalog(books: list[Book]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for idx, book in enumerate(books, start=1):
        score = _compute_book_weight(book.price_cents, idx)
        ranked.append({"id": book.id, "title": book.title, "score": score})
    ranked.sort(key=lambda item: -item["score"])
    return ranked


def create_app(
    db_uri: str = "sqlite:///:memory:", lens_db: str = ".flask_lens_demo.sqlite3"
) -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["LENS_STORAGE"] = "memory" if lens_db == ":memory:" else "sqlite"
    app.config["LENS_DB_PATH"] = lens_db
    app.config["LENS_SLOW_QUERY_MS"] = 5.0
    app.config["LENS_PROFILE_MODE"] = "always"
    app.config["LENS_APP_ROOT"] = os.path.dirname(os.path.abspath(__file__))

    db.init_app(app)
    lens.init_app(app, db=db)

    @app.get("/")
    def home() -> Response:
        html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>flask-lens Bookstore Demo</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0b0c0e;color:#e7eaf0;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.5}
    h1{font-size:22px;margin-bottom:6px}
    p{color:#98a2b3}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:24px 0}
    button,a.cta{display:block;text-align:left;padding:12px 14px;border-radius:8px;border:1px solid #222631;background:#111317;color:#e7eaf0;cursor:pointer;text-decoration:none}
    button:hover{background:#1c202a;border-color:#38bdf8}
    a.cta{background:#38bdf8;color:#081018;font-weight:600;text-align:center;margin-bottom:20px;border-color:#38bdf8}
    .title{font-weight:600;margin-bottom:2px}
    .sub{font-size:12px;color:#98a2b3}
    #out{padding:12px;border-radius:8px;background:#111317;border:1px solid #222631;font-family:monospace;font-size:12px;white-space:pre-wrap;min-height:48px}
  </style>
</head>
<body>
  <h1>flask-lens Bookstore Demo</h1>
  <p>Trigger realistic backend workloads below, then inspect SQL N+1 traces, EXPLAIN plans, cProfile flamegraphs, and exceptions inside <b>/__lens</b>.</p>
  <a class="cta" href="/__lens" target="_blank">Open flask-lens Console (/__lens) →</a>
  <div class="grid">
    <button onclick="hit('GET','/api/authors')"><div class="title">1. Classic N+1 Bug</div><div class="sub">GET /api/authors (13 SQL queries)</div></button>
    <button onclick="hit('GET','/api/authors?optimized=1')"><div class="title">2. Optimized Eager Load</div><div class="sub">GET /api/authors?optimized=1 (2 SQL queries)</div></button>
    <button onclick="hit('GET','/api/books/search?q=Systems')"><div class="title">3. Full Table Scan (EXPLAIN)</div><div class="sub">GET /api/books/search?q=Systems</div></button>
    <button onclick="hit('GET','/api/orders/report')"><div class="title">4. CPU Flamegraph + Spans</div><div class="sub">GET /api/orders/report</div></button>
    <button onclick="hit('POST','/api/orders',{book_id:1,quantity:2,api_key:'sk_live_secret_999'})"><div class="title">5. Write + Secret Redaction</div><div class="sub">POST /api/orders (redacts auth &amp; api_key)</div></button>
    <button onclick="hit('GET','/api/boom')"><div class="title">6. Unhandled Exception</div><div class="sub">GET /api/boom (locals + traceback)</div></button>
  </div>
  <pre id="out">Click any scenario above to fire a request...</pre>
  <script>
    async function hit(method, url, body) {
      const opts = { method, headers: { 'Authorization': 'Bearer demo-secret-token-42' } };
      if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
      const res = await fetch(url, opts);
      const txt = await res.text();
      document.getElementById('out').textContent = `${method} ${url} -> HTTP ${res.status}\\nServer-Timing: ${res.headers.get('Server-Timing')}\\n\\n${txt.slice(0, 500)}`;
    }
  </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")

    @app.get("/api/authors")
    def list_authors() -> Response:
        optimized = request.args.get("optimized") in {"1", "true", "yes"}
        if optimized:
            stmt = select(Author).options(selectinload(Author.books)).order_by(Author.id)
            logger.info("Fetching authors with selectinload eager loading")
        else:
            stmt = select(Author).order_by(Author.id)
            logger.warning("Fetching authors without eager loading (N+1 risk)")

        authors = db.session.scalars(stmt).all()
        payload = [
            {
                "id": author.id,
                "name": author.name,
                "country": author.country,
                "books": [
                    {"id": b.id, "title": b.title, "price_cents": b.price_cents}
                    for b in author.books
                ],
            }
            for author in authors
        ]
        return jsonify({"count": len(payload), "optimized": optimized, "authors": payload})

    @app.get("/api/books/search")
    def search_books() -> Response:
        q = request.args.get("q", "Systems")
        stmt = select(Book).where(Book.title.like(f"%{q}%")).order_by(Book.price_cents.desc())
        books = db.session.scalars(stmt).all()
        return jsonify(
            {
                "query": q,
                "count": len(books),
                "books": [
                    {"id": b.id, "title": b.title, "price_cents": b.price_cents} for b in books
                ],
            }
        )

    @app.get("/api/orders/report")
    @lens.profile
    def orders_report() -> Response:
        logger.info("Building bookstore catalog report")
        books = list(db.session.scalars(select(Book)).all())
        with lens.span("catalog.scoring", book_count=len(books)) as meta:
            ranked = _rank_catalog(books)
            meta["top_book"] = ranked[0]["title"] if ranked else None
        lens.event("report.generated", {"ranked_items": len(ranked)})
        return jsonify({"total_books": len(books), "top_5": ranked[:5]})

    @app.post("/api/orders")
    def create_order() -> tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        book_id = int(body.get("book_id", 1))
        quantity = int(body.get("quantity", 1))
        book = db.session.get(Book, book_id)
        if book is None:
            return jsonify({"error": "Book not found"}), 404

        with lens.span("payment.authorize", provider="stripe-sim"):
            time.sleep(0.004)
            order = Order(
                book_id=book.id, quantity=quantity, total_cents=book.price_cents * quantity
            )
            db.session.add(order)
            db.session.commit()

        logger.info("Created order %s for book %s", order.id, book.title)
        lens.event("order.created", {"order_id": order.id, "total_cents": order.total_cents})
        return jsonify({"id": order.id, "book": book.title, "total_cents": order.total_cents}), 201

    @app.get("/api/boom")
    def boom() -> Response:
        stripe_api_key = "sk_live_never_expose_this_value"  # noqa: F841
        discount_tiers = [10, 20, 0]
        subtotal = 4900
        logger.error("Attempting discount calculation with zero divisor")
        ratio = subtotal / discount_tiers[2]
        return jsonify({"ratio": ratio})

    with app.app_context():
        db.create_all()
        seed_database()

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo_app = create_app(db_uri="sqlite:///bookstore_demo.sqlite3")
    print("Bookstore Demo : http://127.0.0.1:5050/")
    print("Lens Console   : http://127.0.0.1:5050/__lens")
    demo_app.run(host="127.0.0.1", port=5050, debug=False)
