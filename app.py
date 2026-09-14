import os
import sqlite3
import uuid
from datetime import datetime

from flask import Flask, g, render_template, request, redirect, url_for, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "finance.db")

app = Flask(__name__)

MONTH_NAMES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('fixa', 'variavel', 'receber')),
            group_id TEXT,
            installment_index INTEGER,
            installment_total INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


def brl(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    s = f"{value:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


app.jinja_env.filters["brl"] = brl


def month_name(month):
    return MONTH_NAMES[month - 1]


def add_months(year, month, offset):
    total = (year * 12 + (month - 1)) + offset
    new_year = total // 12
    new_month = total % 12 + 1
    return new_year, new_month


def fetch_month_items(year, month):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM items WHERE year = ? AND month = ? ORDER BY id",
        (year, month),
    ).fetchall()
    return rows


def month_totals(year, month):
    rows = fetch_month_items(year, month)
    fixa = sum(r["amount"] for r in rows if r["type"] == "fixa")
    variavel = sum(r["amount"] for r in rows if r["type"] == "variavel")
    receber = sum(r["amount"] for r in rows if r["type"] == "receber")
    despesas = fixa + variavel
    return {
        "fixa": fixa,
        "variavel": variavel,
        "despesas": despesas,
        "receber": receber,
        "saldo": despesas - receber,
    }


@app.route("/")
def index():
    year = request.args.get("ano", default=datetime.now().year, type=int)
    months = []
    for m in range(1, 13):
        totals = month_totals(year, m)
        months.append({"month": m, "name": month_name(m), "totals": totals})
    return render_template("index.html", year=year, months=months)


@app.route("/mes/<int:year>/<int:month>")
def month_view(year, month):
    if month < 1 or month > 12:
        abort(404)
    items = fetch_month_items(year, month)
    totals = month_totals(year, month)
    return render_template(
        "month.html",
        year=year,
        month=month,
        month_name=month_name(month),
        items=items,
        totals=totals,
    )


@app.route("/mes/<int:year>/<int:month>/novo", methods=["POST"])
def new_item(year, month):
    name = (request.form.get("name") or "").strip()
    type_ = request.form.get("type") or "variavel"
    try:
        amount = float(request.form.get("amount", "0").replace(",", "."))
    except ValueError:
        amount = 0.0
    parcelado = request.form.get("parcelado") == "on"
    try:
        parcelas = int(request.form.get("parcelas", "1"))
    except ValueError:
        parcelas = 1

    if not name or amount <= 0:
        return redirect(url_for("month_view", year=year, month=month))

    db = get_db()
    now = datetime.now().isoformat()

    if parcelado and parcelas > 1:
        group_id = str(uuid.uuid4())
        for i in range(parcelas):
            y, m = add_months(year, month, i)
            item_name = f"{name} ({i + 1}/{parcelas})"
            db.execute(
                """
                INSERT INTO items (year, month, name, amount, type, group_id, installment_index, installment_total, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (y, m, item_name, amount, type_, group_id, i + 1, parcelas, now),
            )
    else:
        db.execute(
            """
            INSERT INTO items (year, month, name, amount, type, group_id, installment_index, installment_total, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (year, month, name, amount, type_, now),
        )
    db.commit()
    return redirect(url_for("month_view", year=year, month=month))


@app.route("/item/<int:item_id>/editar", methods=["GET", "POST"])
def edit_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        type_ = request.form.get("type") or "variavel"
        try:
            amount = float(request.form.get("amount", "0").replace(",", "."))
        except ValueError:
            amount = 0.0
        if name and amount > 0:
            db.execute(
                "UPDATE items SET name = ?, amount = ?, type = ? WHERE id = ?",
                (name, amount, type_, item_id),
            )
            db.commit()
        return redirect(url_for("month_view", year=item["year"], month=item["month"]))

    return render_template("edit_item.html", item=item)


@app.route("/item/<int:item_id>/excluir", methods=["POST"])
def delete_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return redirect(url_for("month_view", year=item["year"], month=item["month"]))


@app.route("/item/<int:item_id>/excluir-futuras", methods=["POST"])
def delete_future_installments(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    if item["group_id"]:
        db.execute(
            "DELETE FROM items WHERE group_id = ? AND installment_index >= ?",
            (item["group_id"], item["installment_index"]),
        )
        db.commit()
    return redirect(url_for("month_view", year=item["year"], month=item["month"]))


@app.route("/item/<int:item_id>/replicar", methods=["GET", "POST"])
def replicate_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)

    if request.method == "POST":
        try:
            target_year = int(request.form.get("target_year"))
            target_month = int(request.form.get("target_month"))
        except (TypeError, ValueError):
            return redirect(url_for("replicate_item", item_id=item_id))

        if target_month < 1 or target_month > 12:
            return redirect(url_for("replicate_item", item_id=item_id))

        now = datetime.now().isoformat()
        db.execute(
            """
            INSERT INTO items (year, month, name, amount, type, group_id, installment_index, installment_total, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (target_year, target_month, item["name"], item["amount"], item["type"], now),
        )
        db.commit()
        return redirect(url_for("month_view", year=target_year, month=target_month))

    return render_template(
        "replicate_item.html",
        item=item,
        origin_month_name=month_name(item["month"]),
        month_options=[(m, month_name(m)) for m in range(1, 13)],
    )

@app.route("/replicar-varias", methods=["POST"])
def bulk_replicate_select():
    item_ids = request.form.getlist("item_ids")
    origin_year = request.form.get("origin_year", type=int)
    origin_month = request.form.get("origin_month", type=int)

    if not origin_year or not origin_month:
        return redirect(url_for("index"))
    if not item_ids:
        return redirect(url_for("month_view", year=origin_year, month=origin_month))

    db = get_db()
    placeholders = ",".join("?" for _ in item_ids)
    items = db.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()

    return render_template(
        "bulk_replicate.html",
        items=items,
        origin_year=origin_year,
        origin_month=origin_month,
        month_options=[(m, month_name(m)) for m in range(1, 13)],
    )


@app.route("/replicar-varias/confirmar", methods=["POST"])
def bulk_replicate_confirm():
    item_ids = request.form.getlist("item_ids")
    target_year = request.form.get("target_year", type=int)
    target_months = request.form.getlist("target_months")
    origin_year = request.form.get("origin_year", type=int)
    origin_month = request.form.get("origin_month", type=int)

    if item_ids and target_year and target_months:
        db = get_db()
        placeholders = ",".join("?" for _ in item_ids)
        items = db.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
        now = datetime.now().isoformat()
        for item in items:
            for tm in target_months:
                tm = int(tm)
                db.execute(
                    """
                    INSERT INTO items (year, month, name, amount, type, group_id, installment_index, installment_total, created_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    """,
                    (target_year, tm, item["name"], item["amount"], item["type"], now),
                )
        db.commit()

    if origin_year and origin_month:
        return redirect(url_for("month_view", year=origin_year, month=origin_month))
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
