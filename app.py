import os
import sqlite3
import uuid
from datetime import datetime

from flask import (
    Flask,
    g,
    render_template,
    request,
    redirect,
    url_for,
    abort,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "finance.db")

app = Flask(__name__)


MONTH_NAMES = [
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]


# ============================================================
# DATABASE
# ============================================================

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


def table_columns(db, table_name):
    rows = db.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {row["name"] for row in rows}


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # --------------------------------------------------------
    # Tabela principal de lançamentos
    # --------------------------------------------------------

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL
                CHECK(type IN ('fixa', 'variavel', 'receber')),
            group_id TEXT,
            installment_index INTEGER,
            installment_total INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )

    # --------------------------------------------------------
    # Cartões
    # --------------------------------------------------------

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )

    # --------------------------------------------------------
    # Migração de items existentes
    # --------------------------------------------------------

    columns = table_columns(db, "items")

    if "link" not in columns:
        db.execute(
            "ALTER TABLE items ADD COLUMN link TEXT"
        )

    if "card_id" not in columns:
        db.execute(
            "ALTER TABLE items ADD COLUMN card_id INTEGER"
        )

    if "invoice_id" not in columns:
        db.execute(
            "ALTER TABLE items ADD COLUMN invoice_id INTEGER"
        )

    if "is_invoice" not in columns:
        db.execute(
            """
            ALTER TABLE items
            ADD COLUMN is_invoice INTEGER NOT NULL DEFAULT 0
            """
        )

    if "estimated" not in columns:
        db.execute(
            """
            ALTER TABLE items
            ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0
            """
        )

    # --------------------------------------------------------
    # Status de pagamento
    #
    # 0 = não pago
    # 1 = pago
    # --------------------------------------------------------

    if "paid" not in columns:
        db.execute(
            """
            ALTER TABLE items
            ADD COLUMN paid INTEGER NOT NULL DEFAULT 0
            """
        )

    # --------------------------------------------------------
    # Cartão Inter inicial
    #
    # Se nenhum cartão existir, cria automaticamente o Inter.
    # --------------------------------------------------------

    card_count = db.execute(
        "SELECT COUNT(*) AS total FROM cards"
    ).fetchone()["total"]

    if card_count == 0:
        db.execute(
            """
            INSERT INTO cards (name, created_at)
            VALUES (?, ?)
            """,
            (
                "Inter",
                datetime.now().isoformat(),
            ),
        )

    db.commit()
    db.close()


# ============================================================
# HELPERS
# ============================================================

def brl(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0

    s = f"{value:,.2f}"

    s = (
        s
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )

    return f"R$ {s}"


app.jinja_env.filters["brl"] = brl


def month_name(month):
    return MONTH_NAMES[month - 1]


def add_months(year, month, offset):
    total = (year * 12 + (month - 1)) + offset

    new_year = total // 12
    new_month = total % 12 + 1

    return new_year, new_month


def parse_amount(value):
    try:
        value = (value or "").strip()

        if "," in value:
            value = value.replace(".", "").replace(",", ".")

        return float(value)

    except (TypeError, ValueError):
        return 0.0


# ============================================================
# PAGAMENTOS
# ============================================================

def month_payment_status(year, month):
    """
    Determina se todas as contas relevantes do mês estão pagas.

    São considerados:
    - contas fixas;
    - contas variáveis;
    - contas A receber;
    - faturas com valor maior que zero.

    Compras vinculadas a uma fatura NÃO são consideradas
    separadamente, pois o pagamento delas é representado
    pela própria fatura.

    Faturas vazias, com valor igual a zero, não são
    consideradas no status do mês.

    Um mês sem nenhuma conta não é considerado pago.
    """

    db = get_db()

    result = db.execute(
        """
        SELECT
            COUNT(*) AS total,

            COALESCE(
                SUM(
                    CASE
                        WHEN paid = 1 THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS paid_count

        FROM items

        WHERE year = ?
          AND month = ?

          AND (
              (
                  invoice_id IS NULL
                  AND is_invoice = 0
              )

              OR

              (
                  is_invoice = 1
                  AND amount > 0
              )
          )
        """,
        (
            year,
            month,
        ),
    ).fetchone()

    total = result["total"] or 0
    paid_count = result["paid_count"] or 0
    pending_count = total - paid_count

    is_paid = (
        total > 0
        and pending_count == 0
    )

    return {
        "total": total,
        "paid_count": paid_count,
        "pending_count": pending_count,

        # Nome utilizado pelas rotas existentes.
        "paid": is_paid,

        # Nome utilizado pelo month.html.
        "is_paid": is_paid,
    }


# ============================================================
# CARTÕES
# ============================================================

def get_cards():
    db = get_db()

    return db.execute(
        """
        SELECT *
        FROM cards
        ORDER BY name
        """
    ).fetchall()


def get_card(card_id):
    if not card_id:
        return None

    db = get_db()

    return db.execute(
        """
        SELECT *
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()


# ============================================================
# FATURAS
# ============================================================

def get_invoice(card_id, year, month):
    """
    Procura a fatura de um cartão em determinado mês.
    """

    db = get_db()

    return db.execute(
        """
        SELECT *
        FROM items
        WHERE is_invoice = 1
          AND card_id = ?
          AND year = ?
          AND month = ?
        ORDER BY id
        LIMIT 1
        """,
        (
            card_id,
            year,
            month,
        ),
    ).fetchone()


def create_invoice(
    card_id,
    year,
    month,
    initial_amount=0,
    estimated=True,
):
    """
    Cria automaticamente uma fatura.

    A fatura é armazenada como um item especial.
    Ela entra no cálculo financeiro.
    As compras vinculadas a ela não entram novamente.
    """

    db = get_db()

    card = get_card(card_id)

    if card is None:
        return None

    existing = get_invoice(
        card_id,
        year,
        month,
    )

    if existing:
        return existing

    now = datetime.now().isoformat()

    invoice = db.execute(
        """
        INSERT INTO items (
            year,
            month,
            name,
            amount,
            type,
            group_id,
            installment_index,
            installment_total,
            created_at,
            link,
            card_id,
            invoice_id,
            is_invoice,
            estimated,
            paid
        )
        VALUES (
            ?, ?, ?, ?, 'fixa',
            NULL, NULL, NULL, ?,
            NULL, ?, NULL, 1, ?, 0
        )
        """,
        (
            year,
            month,
            f"Fatura {card['name']}",
            initial_amount,
            now,
            card_id,
            1 if estimated else 0,
        ),
    )

    invoice_id = invoice.lastrowid

    db.commit()

    return db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (invoice_id,),
    ).fetchone()


def get_or_create_invoice(
    card_id,
    year,
    month,
    initial_amount=0,
):
    invoice = get_invoice(
        card_id,
        year,
        month,
    )

    if invoice:
        return invoice

    return create_invoice(
        card_id=card_id,
        year=year,
        month=month,
        initial_amount=initial_amount,
        estimated=True,
    )


def add_amount_to_invoice(
    card_id,
    year,
    month,
    amount,
):
    """
    Adiciona uma parcela à fatura.

    Se a fatura não existir, ela é criada automaticamente.

    Ao adicionar uma nova compra, a fatura volta para
    pendente caso estivesse anteriormente marcada como paga.
    """

    if not card_id or amount <= 0:
        return None

    db = get_db()

    invoice = get_or_create_invoice(
        card_id=card_id,
        year=year,
        month=month,
        initial_amount=0,
    )

    db.execute(
        """
        UPDATE items
        SET
            amount = amount + ?,
            paid = 0
        WHERE id = ?
        """,
        (
            amount,
            invoice["id"],
        ),
    )

    db.commit()

    return db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (invoice["id"],),
    ).fetchone()


def remove_amount_from_invoice(
    invoice_id,
    amount,
):
    """
    Remove o valor de uma parcela da fatura.

    A fatura nunca fica negativa.
    """

    if not invoice_id or amount <= 0:
        return

    db = get_db()

    invoice = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
          AND is_invoice = 1
        """,
        (invoice_id,),
    ).fetchone()

    if invoice is None:
        return

    new_amount = max(
        0,
        invoice["amount"] - amount,
    )

    db.execute(
        """
        UPDATE items
        SET
            amount = ?,
            paid = CASE
                WHEN ? <= 0 THEN 0
                ELSE paid
            END
        WHERE id = ?
        """,
        (
            new_amount,
            new_amount,
            invoice_id,
        ),
    )

    db.commit()


def update_invoice_amount(
    invoice_id,
    amount,
):
    """
    Altera manualmente o valor da fatura.

    Ao fazer isso, ela deixa de ser uma previsão
    e volta para pendente, pois o valor da obrigação
    foi alterado.
    """

    db = get_db()

    db.execute(
        """
        UPDATE items
        SET
            amount = ?,
            estimated = 0,
            paid = 0
        WHERE id = ?
          AND is_invoice = 1
        """,
        (
            amount,
            invoice_id,
        ),
    )

    db.commit()


# ============================================================
# ITENS
# ============================================================

def fetch_month_items(year, month):
    db = get_db()

    return db.execute(
        """
        SELECT
            items.*,
            cards.name AS card_name,
            invoices.name AS invoice_name
        FROM items

        LEFT JOIN cards
            ON cards.id = items.card_id

        LEFT JOIN items AS invoices
            ON invoices.id = items.invoice_id

        WHERE items.year = ?
          AND items.month = ?

        ORDER BY
            items.is_invoice DESC,
            items.id
        """,
        (
            year,
            month,
        ),
    ).fetchall()


def fetch_month_invoices(year, month):
    db = get_db()

    return db.execute(
        """
        SELECT
            items.*,
            cards.name AS card_name
        FROM items

        JOIN cards
            ON cards.id = items.card_id

        WHERE items.year = ?
          AND items.month = ?
          AND items.is_invoice = 1

        ORDER BY items.id
        """,
        (
            year,
            month,
        ),
    ).fetchall()


def month_totals(year, month):
    rows = fetch_month_items(
        year,
        month,
    )

    # --------------------------------------------------------
    # Faturas entram no cálculo.
    #
    # Itens vinculados a uma fatura não entram novamente,
    # porque já estão representados no valor da fatura.
    # --------------------------------------------------------

    contabilizados = [
        row
        for row in rows
        if row["invoice_id"] is None
    ]

    fixa = sum(
        row["amount"]
        for row in contabilizados
        if row["type"] == "fixa"
    )

    variavel = sum(
        row["amount"]
        for row in contabilizados
        if row["type"] == "variavel"
    )

    receber = sum(
        row["amount"]
        for row in contabilizados
        if row["type"] == "receber"
    )

    despesas = fixa + variavel

    return {
        "fixa": fixa,
        "variavel": variavel,
        "despesas": despesas,
        "receber": receber,
        "saldo": despesas - receber,
    }


# ============================================================
# INDEX
# ============================================================

@app.route("/")
def index():
    year = request.args.get(
        "ano",
        default=datetime.now().year,
        type=int,
    )

    months = []

    for month in range(1, 13):
        totals = month_totals(
            year,
            month,
        )

        payment_status = month_payment_status(
            year,
            month,
        )

        months.append(
            {
                "month": month,
                "name": month_name(month),
                "totals": totals,
                "payment_status": payment_status,
            }
        )

    return render_template(
        "index.html",
        year=year,
        months=months,
    )


# ============================================================
# VISUALIZAÇÃO DO MÊS
# ============================================================

@app.route("/mes/<int:year>/<int:month>")
def month_view(year, month):
    if month < 1 or month > 12:
        abort(404)

    items = fetch_month_items(
        year,
        month,
    )

    invoices = fetch_month_invoices(
        year,
        month,
    )

    totals = month_totals(
        year,
        month,
    )

    cards = get_cards()

    payment_status = month_payment_status(
        year,
        month,
    )

    return render_template(
        "month.html",
        year=year,
        month=month,
        month_name=month_name(month),
        items=items,
        invoices=invoices,
        cards=cards,
        totals=totals,
        payment_status=payment_status,
    )


# ============================================================
# NOVO ITEM
# ============================================================

@app.route(
    "/mes/<int:year>/<int:month>/novo",
    methods=["POST"],
)
def new_item(year, month):
    name = (
        request.form.get("name") or ""
    ).strip()

    type_ = (
        request.form.get("type")
        or "variavel"
    )

    amount = parse_amount(
        request.form.get("amount")
    )

    link = (
        request.form.get("link") or ""
    ).strip()

    parcelado = (
        request.form.get("parcelado")
        == "on"
    )

    try:
        parcelas = int(
            request.form.get(
                "parcelas",
                "1",
            )
        )
    except ValueError:
        parcelas = 1

    card_id = request.form.get(
        "card_id",
        type=int,
    )
    
    if parcelado and not card_id:
        return redirect(
            url_for(
                "month_view",
                year=year,
                month=month,
            )
        )

    if not name or amount <= 0:
        return redirect(
            url_for(
                "month_view",
                year=year,
                month=month,
            )
        )

    # Só compras parceladas usam cartão.
    if not parcelado:
        card_id = None

    # Se marcou parcelado mas não escolheu cartão,
    # ainda permitimos cadastrar como parcelamento normal.
    if parcelado and parcelas >= 1:
        group_id = str(uuid.uuid4())

        db = get_db()
        now = datetime.now().isoformat()

        for i in range(parcelas):
            item_year, item_month = add_months(
                year,
                month,
                i,
            )

            invoice_id = None

            # ------------------------------------------------
            # Se existe cartão:
            #
            # 1. encontra a fatura;
            # 2. se não existir, cria;
            # 3. adiciona o valor da parcela;
            # 4. vincula a parcela à fatura.
            # ------------------------------------------------

            if card_id:
                invoice = get_or_create_invoice(
                    card_id=card_id,
                    year=item_year,
                    month=item_month,
                    initial_amount=0,
                )

                db.execute(
                    """
                    UPDATE items
                    SET
                        amount = amount + ?,
                        paid = 0
                    WHERE id = ?
                    """,
                    (
                        amount,
                        invoice["id"],
                    ),
                )

                invoice_id = invoice["id"]

            item_name = (
                f"{name} ({i + 1}/{parcelas})"
            )

            db.execute(
                """
                INSERT INTO items (
                    year,
                    month,
                    name,
                    amount,
                    type,
                    group_id,
                    installment_index,
                    installment_total,
                    created_at,
                    link,
                    card_id,
                    invoice_id,
                    is_invoice,
                    estimated,
                    paid
                )
                VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, 0, 0, 0
                )
                """,
                (
                    item_year,
                    item_month,
                    item_name,
                    amount,
                    type_,
                    group_id,
                    i + 1,
                    parcelas,
                    now,
                    link,
                    card_id,
                    invoice_id,
                ),
            )

        db.commit()

    else:
        db = get_db()
        now = datetime.now().isoformat()

        db.execute(
            """
            INSERT INTO items (
                year,
                month,
                name,
                amount,
                type,
                group_id,
                installment_index,
                installment_total,
                created_at,
                link,
                card_id,
                invoice_id,
                is_invoice,
                estimated,
                paid
            )
            VALUES (
                ?, ?, ?, ?, ?,
                NULL, NULL, NULL, ?,
                ?, NULL, NULL, 0, 0, 0
            )
            """,
            (
                year,
                month,
                name,
                amount,
                type_,
                now,
                link,
            ),
        )

        db.commit()

    return redirect(
        url_for(
            "month_view",
            year=year,
            month=month,
        )
    )


# ============================================================
# PAGAR / DESPAGAR ITEM
# ============================================================

@app.route(
    "/item/<int:item_id>/toggle-pago",
    methods=["POST"],
)
def toggle_item_paid(item_id):
    db = get_db()

    item = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        abort(404)

    new_paid = 0 if item["paid"] else 1

    db.execute(
        """
        UPDATE items
        SET paid = ?
        WHERE id = ?
        """,
        (
            new_paid,
            item_id,
        ),
    )

    db.commit()

    return redirect(
        url_for(
            "month_view",
            year=item["year"],
            month=item["month"],
        )
    )


# ============================================================
# PAGAR / DESPAGAR MÊS
# ============================================================

@app.route(
    "/mes/<int:year>/<int:month>/toggle-pago",
    methods=["POST"],
)
def toggle_month_paid(year, month):
    if month < 1 or month > 12:
        abort(404)

    db = get_db()

    status = month_payment_status(
        year,
        month,
    )

    new_paid = 0 if status["paid"] else 1

    db.execute(
        """
        UPDATE items

        SET paid = ?

        WHERE year = ?
          AND month = ?

          AND (
              (
                  invoice_id IS NULL
                  AND is_invoice = 0
              )

              OR

              (
                  is_invoice = 1
                  AND amount > 0
              )
          )
        """,
        (
            new_paid,
            year,
            month,
        ),
    )

    db.commit()

    return redirect(
        url_for(
            "month_view",
            year=year,
            month=month,
        )
    )


# ============================================================
# EDITAR ITEM
# ============================================================

@app.route(
    "/item/<int:item_id>/editar",
    methods=["GET", "POST"],
)
def edit_item(item_id):
    db = get_db()

    item = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        abort(404)

    # --------------------------------------------------------
    # Não permitimos editar a fatura como item comum.
    # Existe uma rota específica para isso.
    # --------------------------------------------------------

    if item["is_invoice"]:
        if request.method == "POST":
            amount = parse_amount(
                request.form.get("amount")
            )

            if amount >= 0:
                update_invoice_amount(
                    item_id,
                    amount,
                )

            return redirect(
                url_for(
                    "month_view",
                    year=item["year"],
                    month=item["month"],
                )
            )

        return render_template(
            "edit_item.html",
            item=item,
            cards=get_cards(),
            is_invoice=True,
        )

    if request.method == "POST":
        name = (
            request.form.get("name") or ""
        ).strip()

        type_ = (
            request.form.get("type")
            or "variavel"
        )

        amount = parse_amount(
            request.form.get("amount")
        )

        link = (
            request.form.get("link") or ""
        ).strip()

        if not name or amount <= 0:
            return redirect(
                url_for(
                    "month_view",
                    year=item["year"],
                    month=item["month"],
                )
            )

        old_amount = item["amount"]
        old_invoice_id = item["invoice_id"]

        # ----------------------------------------------------
        # Se a parcela estiver vinculada a uma fatura,
        # ajustamos o valor da fatura pela diferença.
        # ----------------------------------------------------

        if old_invoice_id:
            difference = amount - old_amount

            if difference != 0:
                invoice = db.execute(
                    """
                    SELECT *
                    FROM items
                    WHERE id = ?
                      AND is_invoice = 1
                    """,
                    (old_invoice_id,),
                ).fetchone()

                if invoice:
                    new_invoice_amount = max(
                        0,
                        invoice["amount"] + difference,
                    )

                    db.execute(
                        """
                        UPDATE items
                        SET
                            amount = ?,
                            paid = 0
                        WHERE id = ?
                        """,
                        (
                            new_invoice_amount,
                            old_invoice_id,
                        ),
                    )

        db.execute(
            """
            UPDATE items
            SET
                name = ?,
                amount = ?,
                type = ?,
                link = ?
            WHERE id = ?
            """,
            (
                name,
                amount,
                type_,
                link,
                item_id,
            ),
        )

        db.commit()

        return redirect(
            url_for(
                "month_view",
                year=item["year"],
                month=item["month"],
            )
        )

    return render_template(
        "edit_item.html",
        item=item,
        cards=get_cards(),
        is_invoice=False,
    )


# ============================================================
# EXCLUIR ITEM
# ============================================================

@app.route(
    "/item/<int:item_id>/excluir",
    methods=["POST"],
)
def delete_item(item_id):
    db = get_db()

    item = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        abort(404)

    # --------------------------------------------------------
    # Se for uma parcela vinculada à fatura,
    # remove o valor da fatura antes de apagar a parcela.
    # --------------------------------------------------------

    if item["invoice_id"]:
        remove_amount_from_invoice(
            item["invoice_id"],
            item["amount"],
        )

    db.execute(
        """
        DELETE FROM items
        WHERE id = ?
        """,
        (item_id,),
    )

    db.commit()

    return redirect(
        url_for(
            "month_view",
            year=item["year"],
            month=item["month"],
        )
    )


# ============================================================
# EXCLUIR PARCELAS FUTURAS
# ============================================================

@app.route(
    "/item/<int:item_id>/excluir-futuras",
    methods=["POST"],
)
def delete_future_installments(item_id):
    db = get_db()

    item = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        abort(404)

    if item["group_id"]:
        future_items = db.execute(
            """
            SELECT *
            FROM items
            WHERE group_id = ?
              AND installment_index >= ?
            """,
            (
                item["group_id"],
                item["installment_index"],
            ),
        ).fetchall()

        # Primeiro ajusta todas as faturas.
        for future_item in future_items:
            if future_item["invoice_id"]:
                remove_amount_from_invoice(
                    future_item["invoice_id"],
                    future_item["amount"],
                )

        # Depois remove as parcelas.
        db.execute(
            """
            DELETE FROM items
            WHERE group_id = ?
              AND installment_index >= ?
            """,
            (
                item["group_id"],
                item["installment_index"],
            ),
        )

        db.commit()

    return redirect(
        url_for(
            "month_view",
            year=item["year"],
            month=item["month"],
        )
    )


# ============================================================
# REPLICAR ITEM
# ============================================================

@app.route(
    "/item/<int:item_id>/replicar",
    methods=["GET", "POST"],
)
def replicate_item(item_id):
    db = get_db()

    item = db.execute(
        """
        SELECT *
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        abort(404)

    if request.method == "POST":
        try:
            target_year = int(
                request.form.get("target_year")
            )

            target_month = int(
                request.form.get("target_month")
            )

        except (TypeError, ValueError):
            return redirect(
                url_for(
                    "replicate_item",
                    item_id=item_id,
                )
            )

        if target_month < 1 or target_month > 12:
            return redirect(
                url_for(
                    "replicate_item",
                    item_id=item_id,
                )
            )

        now = datetime.now().isoformat()

        db.execute(
            """
            INSERT INTO items (
                year,
                month,
                name,
                amount,
                type,
                group_id,
                installment_index,
                installment_total,
                created_at,
                link,
                card_id,
                invoice_id,
                is_invoice,
                estimated,
                paid
            )
            VALUES (
                ?, ?, ?, ?, ?,
                NULL, NULL, NULL, ?,
                ?, NULL, NULL, 0, 0, 0
            )
            """,
            (
                target_year,
                target_month,
                item["name"],
                item["amount"],
                item["type"],
                now,
                item["link"],
            ),
        )

        db.commit()

        return redirect(
            url_for(
                "month_view",
                year=target_year,
                month=target_month,
            )
        )

    return render_template(
        "replicate_item.html",
        item=item,
        origin_month_name=month_name(
            item["month"]
        ),
        month_options=[
            (m, month_name(m))
            for m in range(1, 13)
        ],
    )


# ============================================================
# REPLICAÇÃO EM MASSA
# ============================================================

@app.route(
    "/replicar-varias",
    methods=["POST"],
)
def bulk_replicate_select():
    item_ids = request.form.getlist(
        "item_ids"
    )

    origin_year = request.form.get(
        "origin_year",
        type=int,
    )

    origin_month = request.form.get(
        "origin_month",
        type=int,
    )

    if not origin_year or not origin_month:
        return redirect(
            url_for("index")
        )

    if not item_ids:
        return redirect(
            url_for(
                "month_view",
                year=origin_year,
                month=origin_month,
            )
        )

    db = get_db()

    placeholders = ",".join(
        "?"
        for _ in item_ids
    )

    items = db.execute(
        f"""
        SELECT *
        FROM items
        WHERE id IN ({placeholders})
          AND is_invoice = 0
        """,
        item_ids,
    ).fetchall()

    return render_template(
        "bulk_replicate.html",
        items=items,
        origin_year=origin_year,
        origin_month=origin_month,
        month_options=[
            (m, month_name(m))
            for m in range(1, 13)
        ],
    )


@app.route(
    "/replicar-varias/confirmar",
    methods=["POST"],
)
def bulk_replicate_confirm():
    item_ids = request.form.getlist(
        "item_ids"
    )

    target_year = request.form.get(
        "target_year",
        type=int,
    )

    target_months = request.form.getlist(
        "target_months"
    )

    origin_year = request.form.get(
        "origin_year",
        type=int,
    )

    origin_month = request.form.get(
        "origin_month",
        type=int,
    )

    if (
        item_ids
        and target_year
        and target_months
    ):
        db = get_db()

        placeholders = ",".join(
            "?"
            for _ in item_ids
        )

        items = db.execute(
            f"""
            SELECT *
            FROM items
            WHERE id IN ({placeholders})
              AND is_invoice = 0
            """,
            item_ids,
        ).fetchall()

        now = datetime.now().isoformat()

        for item in items:
            for target_month in target_months:
                target_month = int(
                    target_month
                )

                db.execute(
                    """
                    INSERT INTO items (
                        year,
                        month,
                        name,
                        amount,
                        type,
                        group_id,
                        installment_index,
                        installment_total,
                        created_at,
                        link,
                        card_id,
                        invoice_id,
                        is_invoice,
                        estimated,
                        paid
                    )
                    VALUES (
                        ?, ?, ?, ?, ?,
                        NULL, NULL, NULL, ?,
                        ?, NULL, NULL, 0, 0, 0
                    )
                    """,
                    (
                        target_year,
                        target_month,
                        item["name"],
                        item["amount"],
                        item["type"],
                        now,
                        item["link"],
                    ),
                )

        db.commit()

    if origin_year and origin_month:
        return redirect(
            url_for(
                "month_view",
                year=origin_year,
                month=origin_month,
            )
        )

    return redirect(
        url_for("index")
    )


# ============================================================
# CARTÕES
# ============================================================

@app.route(
    "/cartoes/novo",
    methods=["POST"],
)
def new_card():
    name = (
        request.form.get("name") or ""
    ).strip()

    if name:
        db = get_db()

        try:
            db.execute(
                """
                INSERT INTO cards (
                    name,
                    created_at
                )
                VALUES (?, ?)
                """,
                (
                    name,
                    datetime.now().isoformat(),
                ),
            )

            db.commit()

        except sqlite3.IntegrityError:
            pass

    return redirect(
        request.referrer
        or url_for("index")
    )
    
    
@app.route(
    "/cartoes/<int:card_id>/excluir",
    methods=["POST"],
)
def delete_card(card_id):
    db = get_db()

    card = db.execute(
        """
        SELECT *
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()

    if card is None:
        abort(404)

    usage = db.execute(
        """
        SELECT COUNT(*) AS total
        FROM items
        WHERE card_id = ?
        """,
        (card_id,),
    ).fetchone()

    if usage["total"] > 0:
        return redirect(
            request.referrer
            or url_for("index")
        )

    db.execute(
        """
        DELETE FROM cards
        WHERE id = ?
        """,
        (card_id,),
    )

    db.commit()

    return redirect(
        request.referrer
        or url_for("index")
    )

# ============================================================
# EDITAR FATURA
# ============================================================

@app.route(
    "/fatura/<int:invoice_id>/editar",
    methods=["GET", "POST"],
)
def edit_invoice(invoice_id):
    db = get_db()

    invoice = db.execute(
        """
        SELECT
            items.*,
            cards.name AS card_name

        FROM items

        JOIN cards
            ON cards.id = items.card_id

        WHERE items.id = ?
          AND items.is_invoice = 1
        """,
        (invoice_id,),
    ).fetchone()

    if invoice is None:
        abort(404)

    if request.method == "POST":
        amount = parse_amount(
            request.form.get("amount")
        )

        if amount >= 0:
            update_invoice_amount(
                invoice_id,
                amount,
            )

        return redirect(
            url_for(
                "month_view",
                year=invoice["year"],
                month=invoice["month"],
            )
        )

    return render_template(
        "edit_invoice.html",
        invoice=invoice,
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    init_db()

    app.run(
        host="127.0.0.1",
        port=8080,
        debug=True,
    )