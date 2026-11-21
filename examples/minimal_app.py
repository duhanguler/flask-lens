"""Minimal 15-line quickstart example for flask-lens."""

from __future__ import annotations

from flask import Flask, jsonify

from flask_lens import Lens

app = Flask(__name__)
lens = Lens(app, storage="memory")


@app.get("/api/hello")
def hello() -> tuple[object, int]:
    lens.event("greeting.built", {"target": "world"})
    return jsonify({"message": "Hello from flask-lens!", "dashboard": "/__lens"}), 200


if __name__ == "__main__":
    print("App running at http://127.0.0.1:5050  |  Dashboard: http://127.0.0.1:5050/__lens")
    app.run(host="127.0.0.1", port=5050, debug=False)
