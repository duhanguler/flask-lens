"""Shared pytest fixtures for the flask-lens test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask, Response, jsonify, render_template_string, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from flask_lens import Lens


class Base(DeclarativeBase):
    pass


def build_bookstore_app(
    *,
    storage: str = "memory",
    db_path: str = ":memory:",
    lens_kwargs: dict[str, Any] | None = None,
) -> tuple[Flask, SQLAlchemy, Lens]:
    """Create an isolated Flask + SQLAlchemy + Lens application for testing."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = False

    class LocalBase(DeclarativeBase):
        pass

    db = SQLAlchemy(model_class=LocalBase)

    class Author(db.Model):  # type: ignore[name-defined]
        __tablename__ = "authors"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        name: Mapped[str] = mapped_column(String(100), nullable=False)
        books: Mapped[list[Book]] = relationship("Book", back_populates="author")

    class Book(db.Model):  # type: ignore[name-defined]
        __tablename__ = "books"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        title: Mapped[str] = mapped_column(String(200), nullable=False)
        author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"), nullable=False)
        author: Mapped[Author] = relationship("Author", back_populates="books")

    db.init_app(app)

    options: dict[str, Any] = {
        "storage": storage,
        "db_path": db_path,
        "profile_mode": "always",
        "capture_outgoing_http": False,
        "slow_query_ms": 50.0,
    }
    if lens_kwargs:
        options.update(lens_kwargs)

    lens = Lens(app, db=db, **options)
    logger = logging.getLogger("test_bookstore")

    @app.get("/api/authors")
    def list_authors() -> Response:
        optimized = request.args.get("optimized") == "1"
        if optimized:
            stmt = select(Author).options(selectinload(Author.books)).order_by(Author.id)
        else:
            stmt = select(Author).order_by(Author.id)
        authors = db.session.scalars(stmt).all()
        return jsonify(
            {
                "authors": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "books": [b.title for b in a.books],
                    }
                    for a in authors
                ]
            }
        )

    @app.get("/api/books/search")
    def search_books() -> Response:
        q = request.args.get("q", "Book")
        rows = db.session.scalars(select(Book).where(Book.title.like(f"%{q}%"))).all()
        return jsonify({"count": len(rows), "titles": [b.title for b in rows]})

    @app.post("/api/echo")
    def echo() -> Response:
        logger.info("Echoing JSON payload")
        with lens.span("echo.process", step="validate") as meta:
            payload = request.get_json(silent=True) or {}
            meta["keys"] = len(payload)
        lens.event("echo.done", {"ok": True})
        return jsonify({"received": payload})

    @app.get("/html")
    def html_page() -> str:
        return render_template_string("<h1>Hello {{ name }}</h1>", name="Flask-Lens")

    @app.get("/stream")
    def streamed() -> Response:
        def _gen() -> Iterator[str]:
            yield "chunk-1-"
            yield "chunk-2"

        return Response(_gen(), mimetype="text/plain")

    @app.get("/api/profiled")
    @lens.profile
    def profiled_only() -> Response:
        total = sum(i * i for i in range(1000))
        return jsonify({"total": total})

    @app.get("/api/boom")
    def boom() -> Response:
        api_key = "sk_secret_value_should_redact"  # noqa: F841
        visible_count = 42  # noqa: F841
        raise ZeroDivisionError("cannot divide by zero")

    with app.app_context():
        db.create_all()
        for i in range(1, 7):
            author = Author(name=f"Author {i}")
            db.session.add(author)
            db.session.flush()
            for j in range(1, 3):
                db.session.add(Book(title=f"Book {i}-{j}", author_id=author.id))
        db.session.commit()

    return app, db, lens


@pytest.fixture
def bookstore_bundle() -> Iterator[tuple[Flask, SQLAlchemy, Lens]]:
    app, db, lens = build_bookstore_app(storage="memory")
    try:
        yield app, db, lens
    finally:
        lens.close()
