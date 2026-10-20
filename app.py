import uuid
import os
import secrets
import math
from contextlib import contextmanager
from functools import wraps
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash


# --------------------------------------------------
# Flask configuration
# --------------------------------------------------

app = Flask(__name__)

instance_path = Path(app.instance_path)
instance_path.mkdir(parents=True, exist_ok=True)

secret_file = instance_path / "secret_key"

if secret_file.exists():
    secret_key = secret_file.read_text().strip()
else:
    secret_key = secrets.token_hex(32)
    secret_file.write_text(secret_key)

app.secret_key = os.getenv("FLASK_SECRET_KEY", secret_key)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

CSRFProtect(app)


# --------------------------------------------------
# Database connection
# --------------------------------------------------

def get_connection():
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        return psycopg.connect(
            database_url,
            row_factory=dict_row,
        )

    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "electronic_shop_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        row_factory=dict_row,
    )

@contextmanager
def db_cursor(dictionary=False):
    """Commit successful database operations and roll back failures."""
    conn = get_connection()
    cursor = None

    try:
        if dictionary:
            cursor = conn.cursor(row_factory=dict_row)
        else:
            cursor = conn.cursor()

        yield conn, cursor
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        if cursor is not None:
            cursor.close()
        conn.close()
# --------------------------------------------------
# Login and role protection
# --------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def owner_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))

        if session.get("role") != "owner":
            flash("You do not have permission to access this page.")
            return redirect(url_for("index"))

        return view(*args, **kwargs)
    return wrapped_view


# --------------------------------------------------
# Login
# --------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            with db_cursor(dictionary=True) as (_, cur):
                cur.execute(
                    """
                    SELECT user_id, username, password_hash, role, is_active
                    FROM users
                    WHERE username = %s
                    """,
                    (username,),
                )
                user = cur.fetchone()

            if (
                user
                and user["is_active"]
                and check_password_hash(user["password_hash"], password)
            ):
                session.clear()
                session["user_id"] = user["user_id"]
                session["username"] = user["username"]
                session["role"] = user["role"]

                flash("Login successful.")
                return redirect(url_for("index"))

            flash("Invalid username or password.")

        except psycopg.Error:
            app.logger.exception("Login failed")
            flash("Unable to log in. Please check the database connection.")

    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# --------------------------------------------------
# Dashboard
# --------------------------------------------------

@app.route("/")
@login_required
def index():
    search = request.args.get("q", "").strip()
    like = f"%{search}%"
    is_owner = session.get("role") == "owner"
    low_stock_threshold = 10

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute("SELECT COUNT(*) AS n FROM products")
        total_products = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM customers")
        total_customers = cur.fetchone()["n"]

        revenue = None
        pending = None
        sales = []

        if is_owner:
            cur.execute(
                """
                SELECT COALESCE(SUM(total_amount), 0) AS n
                FROM sales
                WHERE status = 'Completed'
                """
            )
            revenue = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM sales
                WHERE status = 'Pending'
                """
            )
            pending = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT p.product_id, p.product_name, p.brand, p.unit_price,
                   p.stock_quantity, c.category_name
            FROM products AS p
            JOIN categories AS c ON c.category_id = p.category_id
            WHERE p.product_name LIKE %s
               OR p.brand LIKE %s
               OR c.category_name LIKE %s
            ORDER BY p.product_name
            """,
            (like, like, like),
        )
        products = cur.fetchall()

        cur.execute(
            """
            SELECT customer_id, full_name
            FROM customers
            ORDER BY full_name
            """
        )
        customers = cur.fetchall()

        if is_owner:
            cur.execute(
                """
                SELECT
                    s.sale_id,
                    c.full_name AS customer,
                    CASE
                        WHEN COUNT(si.sale_item_id) > 0 THEN
                            STRING_AGG(
    DISTINCT p_item.product_name,
    ', ' ORDER BY p_item.product_name
)
                        ELSE p.product_name
                    END AS product_name,
                    CASE
                        WHEN COUNT(si.sale_item_id) > 0 THEN
                            SUM(si.quantity)
                        ELSE s.quantity
                    END AS quantity,
                    s.total_amount,
                    s.payment_mode,
                    s.status,
                    s.sale_date
                FROM sales AS s
                JOIN customers AS c
                    ON c.customer_id = s.customer_id
                JOIN products AS p
                    ON p.product_id = s.product_id
                LEFT JOIN sale_items AS si
                    ON si.sale_id = s.sale_id
                LEFT JOIN products AS p_item
                    ON p_item.product_id = si.product_id
                GROUP BY
                    s.sale_id, c.full_name, p.product_name,
                    s.quantity, s.total_amount, s.payment_mode,
                    s.status, s.sale_date
                ORDER BY s.sale_id DESC
                LIMIT 10
                """
            )
            sales = cur.fetchall()

    return render_template(
        "index.html",
        products=products,
        customers=customers,
        sales=sales,
        search=search,
        total_products=total_products,
        total_customers=total_customers,
        revenue=revenue,
        pending=pending,
        is_owner=is_owner,
        low_stock_threshold=low_stock_threshold,
    )


# --------------------------------------------------
# Staff management
# --------------------------------------------------

@app.route("/staff")
@owner_required
def staff():
    with db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            """
            SELECT user_id, username, role, is_active
            FROM users
            ORDER BY username
            """
        )
        users = cur.fetchall()

    return render_template("staff.html", users=users)


@app.route("/staff/add", methods=["POST"])
@owner_required
def add_staff():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "cashier")

    if not username or len(password) < 8 or role not in ("owner", "cashier"):
        flash("Enter a username, a password of at least 8 characters, and a valid role.")
        return redirect(url_for("staff"))

    try:
        with db_cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active)
                VALUES (%s, %s, %s, 1)
                """,
                (username, generate_password_hash(password), role),
            )
        flash("Staff account added successfully.")

    except psycopg.IntegrityError:
        flash("That username already exists.")

    except psycopg.Error:
        app.logger.exception("Staff creation failed")
        flash("Could not create the staff account.")

    return redirect(url_for("staff"))


@app.route("/staff/toggle/<int:user_id>", methods=["POST"])
@owner_required
def toggle_staff(user_id):
    if user_id == session.get("user_id"):
        flash("You cannot deactivate your own account.")
        return redirect(url_for("staff"))

    try:
        with db_cursor() as (_, cur):
            cur.execute(
                """
                UPDATE users
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
                WHERE user_id = %s
                """,
                (user_id,),
            )

        flash("Staff account status updated.")

    except psycopg.Error:
        app.logger.exception("Staff status update failed")
        flash("Could not update staff account status.")

    return redirect(url_for("staff"))


# --------------------------------------------------
# Products
# --------------------------------------------------

@app.route("/products")
@login_required
def products_page():
    search = request.args.get("q", "").strip()
    like = f"%{search}%"

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            """
            SELECT p.product_id, p.product_name, p.brand, p.unit_price,
                   p.stock_quantity, c.category_name
            FROM products AS p
            JOIN categories AS c ON c.category_id = p.category_id
            WHERE p.product_name LIKE %s
               OR p.brand LIKE %s
               OR c.category_name LIKE %s
            ORDER BY p.product_name
            """,
            (like, like, like),
        )
        products = cur.fetchall()

    return render_template(
        "products.html",
        products=products,
        search=search,
    )


@app.route("/products/add", methods=["GET", "POST"])
@owner_required
def add_product():
    if request.method == "POST":
        name = request.form.get("product_name", "").strip()
        brand = request.form.get("brand", "").strip()

        try:
            category_id = int(request.form.get("category_id", ""))
            price = float(request.form.get("unit_price", ""))
            stock = int(request.form.get("stock_quantity", ""))

            if not name or category_id < 1 or not math.isfinite(price):
                raise ValueError

            if price < 0 or stock < 0:
                raise ValueError

        except (ValueError, TypeError):
            flash("Enter valid product details.")
            return redirect(url_for("add_product"))

        try:
            with db_cursor() as (_, cur):
                cur.execute(
                    """
                        INSERT INTO products
                        (sku, product_name, brand, category_id, unit_price, stock_quantity)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                    f"SHOP-{uuid.uuid4().hex[:12].upper()}",
                    name,
                    brand,
                    category_id,
                    price,
                    stock,
                    ),
                )

            flash("Product added successfully.")
            return redirect(url_for("products_page"))

        except psycopg.Error:
            app.logger.exception("Product insert failed")
            flash("Could not add the product. Check the category and product details.")

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute("SELECT category_id, category_name FROM categories ORDER BY category_name")
        categories = cur.fetchall()

    return render_template("product_form.html", product=None, categories=categories)


@app.route("/products/edit/<int:product_id>", methods=["GET", "POST"])
@owner_required
def edit_product(product_id):
    if request.method == "POST":
        name = request.form.get("product_name", "").strip()
        brand = request.form.get("brand", "").strip()

        try:
            category_id = int(request.form.get("category_id", ""))
            price = float(request.form.get("unit_price", ""))
            stock = int(request.form.get("stock_quantity", ""))

            if (
                not name or category_id < 1 or not math.isfinite(price)
                or price < 0 or stock < 0
            ):
                raise ValueError

        except (ValueError, TypeError):
            flash("Enter valid product details.")
            return redirect(url_for("edit_product", product_id=product_id))

        try:
            with db_cursor() as (_, cur):
                cur.execute(
                    """
                    UPDATE products
                    SET product_name = %s, brand = %s, category_id = %s,
                        unit_price = %s, stock_quantity = %s
                    WHERE product_id = %s
                    """,
                    (name, brand, category_id, price, stock, product_id),
                )

                if cur.rowcount == 0:
                    cur.execute(
                        "SELECT product_id FROM products WHERE product_id = %s",
                        (product_id,),
                    )
                    if cur.fetchone() is None:
                        flash("Product not found.")
                        return redirect(url_for("products_page"))

            flash("Product updated successfully.")
            return redirect(url_for("products_page"))

        except psycopg.Error:
            app.logger.exception("Product update failed")
            flash("Could not update the product.")

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
        product = cur.fetchone()

        cur.execute("SELECT category_id, category_name FROM categories ORDER BY category_name")
        categories = cur.fetchall()

    if product is None:
        flash("Product not found.")
        return redirect(url_for("products_page"))

    return render_template("product_form.html", product=product, categories=categories)


@app.route("/products/restock/<int:product_id>", methods=["POST"])
@owner_required
def restock_product(product_id):
    try:
        quantity = int(request.form.get("quantity", ""))

        if quantity <= 0:
            raise ValueError

    except (ValueError, TypeError):
        flash("Enter a valid restock quantity.")
        return redirect(url_for("products_page"))

    try:
        with db_cursor() as (_, cur):
            cur.execute(
                """
                UPDATE products
                SET stock_quantity = stock_quantity + %s
                WHERE product_id = %s
                """,
                (quantity, product_id),
            )

            if cur.rowcount == 0:
                flash("Product not found.")
                return redirect(url_for("products_page"))

        flash("Stock updated successfully.")

    except psycopg.Error:
        app.logger.exception("Product restock failed")
        flash("Could not update product stock.")

    return redirect(url_for("products_page"))


# --------------------------------------------------
# Customers
# --------------------------------------------------

@app.route("/customers/add", methods=["POST"])
@login_required
def add_customer():
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip() or None
    phone = request.form.get("phone", "").strip() or None
    address = request.form.get("address", "").strip() or None
    customer_type = request.form.get("customer_type", "Regular")

    if not name:
        flash("Customer name is required.")
        return redirect(url_for("index"))

    if customer_type not in ("Regular", "Premium", "Corporate"):
        flash("Choose a valid customer type.")
        return redirect(url_for("index"))

    try:
        with db_cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO customers
                    (full_name, email, phone, address, customer_type)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (name, email, phone, address, customer_type),
            )

        flash("Customer added successfully.")

    except psycopg.Error:
        app.logger.exception("Customer insert failed")
        flash("Could not add customer. Check that email and phone are unique.")

    return redirect(url_for("index"))


# --------------------------------------------------
# Multi-product billing
# --------------------------------------------------

@app.route("/sales/add", methods=["POST"])
@login_required
def add_sale():
    """Create one invoice with multiple items and update stock atomically."""
    try:
        customer_id = int(request.form.get("customer_id", ""))
        payment_mode = request.form.get("payment_mode", "Cash")

        if customer_id < 1 or payment_mode not in ("Cash", "UPI", "Card", "EMI"):
            raise ValueError

        # Supports forms that submit product_id[] / quantity[] /
        # discount_pct[] as well as a single product.
        product_ids = request.form.getlist("product_id")
        quantities = request.form.getlist("quantity")
        discounts = request.form.getlist("discount_pct")

        if not product_ids:
            product_id = request.form.get("product_id")
            if product_id:
                product_ids = [product_id]

        if not quantities:
            quantity = request.form.get("quantity")
            if quantity:
                quantities = [quantity]

        if not discounts:
            discounts = request.form.getlist("discount_pct")

        if not product_ids or len(product_ids) != len(quantities):
            raise ValueError

        if discounts and len(discounts) not in (1, len(product_ids)):
            raise ValueError

        items = []
        for i, (pid_text, qty_text) in enumerate(zip(product_ids, quantities)):
            pid = int(pid_text)
            qty = int(qty_text)
            discount = float(
                discounts[i] if len(discounts) == len(product_ids)
                else discounts[0] if discounts else 0
            )

            if (
                pid < 1 or qty < 1 or not math.isfinite(discount)
                or not 0 <= discount <= 100
            ):
                raise ValueError

            items.append({
                "product_id": pid,
                "quantity": qty,
                "discount_pct": discount,
            })

        # Prevent duplicate product rows from producing inconsistent stock.
        merged = {}
        for item in items:
            pid = item["product_id"]
            if pid in merged:
                raise ValueError("Add each product only once per invoice.")
            merged[pid] = item
        items = list(merged.values())

    except (ValueError, TypeError, KeyError):
        flash("Enter valid customer, product, quantity, discount, and payment details.")
        return redirect(url_for("index"))

    try:
        with db_cursor() as (_, cur):
            cur.execute(
                "SELECT customer_id FROM customers WHERE customer_id = %s",
                (customer_id,),
            )
            if cur.fetchone() is None:
                raise ValueError("Customer not found.")

            total = 0.0
            invoice_items = []

            # Validate products and compute totals before changing stock.
            for item in items:
                cur.execute(
                    """
                    SELECT product_name, unit_price, stock_quantity
                    FROM products
                    WHERE product_id = %s
                    FOR UPDATE
                    """,
                    (item["product_id"],),
                )
                product = cur.fetchone()

                if product is None:
                    raise ValueError("A selected product was not found.")

                name, unit_price, stock = product
                if stock < item["quantity"]:
                    raise ValueError(f"Not enough stock for {name}.")

                line_total = round(
                    float(unit_price) * item["quantity"]
                    * (1 - item["discount_pct"] / 100),
                    2,
                )
                total += line_total

                invoice_items.append({
                    **item,
                    "unit_price": unit_price,
                    "line_total": line_total,
                })

            total = round(total, 2)
            first_item = invoice_items[0]

            # The legacy sales table requires product_id and quantity.
            cur.execute(
                """
                INSERT INTO sales
                    (customer_id, product_id, quantity, discount_pct,
                     total_amount, payment_mode, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'Completed')
RETURNING sale_id
                """,
                (
                    customer_id,
                    first_item["product_id"],
                    first_item["quantity"],
                    first_item["discount_pct"],
                    total,
                    payment_mode,
                ),
            )
            sale_id = cur.fetchone()[0]

            for item in invoice_items:
                cur.execute(
                    """
                    UPDATE products
                    SET stock_quantity = stock_quantity - %s
                    WHERE product_id = %s AND stock_quantity >= %s
                    """,
                    (
                        item["quantity"],
                        item["product_id"],
                        item["quantity"],
                    ),
                )

                if cur.rowcount != 1:
                    raise ValueError("Stock changed during billing. Please try again.")

                cur.execute(
                    """
                    INSERT INTO sale_items
                        (sale_id, product_id, quantity, unit_price, discount_pct)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        sale_id,
                        item["product_id"],
                        item["quantity"],
                        item["unit_price"],
                        item["discount_pct"],
                    ),
                )

        flash(f"Sale #{sale_id} recorded successfully. Total: Rs. {total:,.2f}")
        return redirect(url_for("sale_receipt", sale_id=sale_id))

    except ValueError as exc:
        flash(str(exc))

    except psycopg.Error:
        app.logger.exception("Sale failed")
        flash("Could not record the sale. No partial sale should be saved.")

    return redirect(url_for("index"))


# --------------------------------------------------
# Receipt
# --------------------------------------------------

@app.route("/sales/<int:sale_id>/receipt")
@login_required
def sale_receipt(sale_id):
    with db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            """
            SELECT s.sale_id, s.sale_date, s.quantity, s.discount_pct,
                   s.total_amount, s.payment_mode, s.status,
                   c.full_name AS customer_name,
                   p.product_name, p.brand, p.unit_price
            FROM sales AS s
            JOIN customers AS c ON c.customer_id = s.customer_id
            JOIN products AS p ON p.product_id = s.product_id
            WHERE s.sale_id = %s
            """,
            (sale_id,),
        )
        receipt = cur.fetchone()

        if receipt is None:
            flash("Receipt not found.")
            return redirect(url_for("index"))

        cur.execute(
            """
            SELECT si.product_id, p.product_name, p.brand,
                   si.quantity, si.unit_price, si.discount_pct,
                   ROUND(
                       si.quantity * si.unit_price
                       * (1 - si.discount_pct / 100), 2
                   ) AS line_total
            FROM sale_items AS si
            JOIN products AS p ON p.product_id = si.product_id
            WHERE si.sale_id = %s
            ORDER BY si.sale_item_id
            """,
            (sale_id,),
        )
        receipt_items = cur.fetchall()

    # Older invoices may not have sale_items records.
    if not receipt_items:
        receipt_items = [{
            "product_id": receipt["product_name"],
            "product_name": receipt["product_name"],
            "brand": receipt["brand"],
            "quantity": receipt["quantity"],
            "unit_price": receipt["unit_price"],
            "discount_pct": receipt["discount_pct"],
            "line_total": receipt["total_amount"],
        }]

    return render_template(
        "receipt.html",
        receipt=receipt,
        items=receipt_items,
    )


# --------------------------------------------------
# Sales history
# --------------------------------------------------

@app.route("/sales/history")
@owner_required
def sales_history():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    status = request.args.get("status", "All").strip()

    allowed_statuses = ("All", "Completed", "Pending", "Cancelled")

    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None

        if (start and start.isoformat() != start_date) or (
            end and end.isoformat() != end_date
        ):
            raise ValueError

        if start and end and start > end:
            raise ValueError

    except ValueError:
        flash("Please enter a valid date range.")
        return redirect(url_for("sales_history"))

    if status not in allowed_statuses:
        status = "All"

    conditions = []
    params = []

    if start:
        conditions.append("s.sale_date >= %s")
        params.append(start)

    if end:
        conditions.append("s.sale_date < (%s::date + INTERVAL '1 day')")
        params.append(end)

    if status != "All":
        conditions.append("s.status = %s")
        params.append(status)

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions else ""
    )

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            f"""
            SELECT
                s.sale_id,
                s.sale_date,
                c.full_name AS customer,
                CASE
                    WHEN COUNT(si.sale_item_id) > 0 THEN
                        STRING_AGG(
    DISTINCT p_item.product_name,
    ', ' ORDER BY p_item.product_name
)
                    ELSE p.product_name
                END AS product_name,
                CASE
                    WHEN COUNT(si.sale_item_id) > 0 THEN SUM(si.quantity)
                    ELSE s.quantity
                END AS quantity,
                s.discount_pct,
                s.total_amount,
                s.payment_mode,
                s.status
            FROM sales AS s
            JOIN customers AS c ON c.customer_id = s.customer_id
            JOIN products AS p ON p.product_id = s.product_id
            LEFT JOIN sale_items AS si ON si.sale_id = s.sale_id
            LEFT JOIN products AS p_item ON p_item.product_id = si.product_id
            {where_clause}
            GROUP BY
                s.sale_id, s.sale_date, c.full_name, p.product_name,
                s.quantity, s.discount_pct, s.total_amount,
                s.payment_mode, s.status
            ORDER BY s.sale_date DESC, s.sale_id DESC
            """,
            tuple(params),
        )
        history = cur.fetchall()

    return render_template(
        "sales_history.html",
        sales=history,
        start_date=start_date,
        end_date=end_date,
        selected_status=status,
    )


# --------------------------------------------------
# Reports
# --------------------------------------------------

@app.route("/reports")
@owner_required
def reports():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None

        if (start and start.isoformat() != start_date) or (
            end and end.isoformat() != end_date
        ):
            raise ValueError

        if start and end and start > end:
            raise ValueError

    except ValueError:
        flash("Please enter a valid date range.")
        return redirect(url_for("reports"))

    conditions = ["s.status = 'Completed'"]
    params = []

    if start:
        conditions.append("s.sale_date >= %s")
        params.append(start)

    if end:
        conditions.append("s.sale_date < (%s::date + INTERVAL '1 day')")
        params.append(end)

    where_clause = "WHERE " + " AND ".join(conditions)

    with db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            f"""
            SELECT COUNT(*) AS total_sales,
                   COALESCE(SUM(s.total_amount), 0) AS total_revenue
            FROM sales AS s
            {where_clause}
            """,
            tuple(params),
        )
        summary = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS total_products,
                   COALESCE(SUM(stock_quantity), 0) AS total_stock,
                   COALESCE(SUM(stock_quantity * unit_price), 0) AS stock_value
            FROM products
            """
        )
        inventory = cur.fetchone()

        cur.execute(
            """
            SELECT p.product_id, p.product_name, p.stock_quantity,
                   p.unit_price, c.category_name
            FROM products AS p
            JOIN categories AS c ON c.category_id = p.category_id
            WHERE p.stock_quantity <= 10
            ORDER BY p.stock_quantity, p.product_name
            """
        )
        low_stock_products = cur.fetchall()

    return render_template(
        "reports.html",
        summary=summary,
        inventory=inventory,
        low_stock_products=low_stock_products,
        start_date=start_date,
        end_date=end_date,
    )


# --------------------------------------------------
# Run application
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )
#$env:DB_PASSWORD = Read-Host "Enter your MySQL root password"