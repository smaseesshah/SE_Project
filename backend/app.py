import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
STATIC_DIR = PROJECT_ROOT / "static"
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATABASE = PROJECT_ROOT / "instance" / "luxetime.sqlite3"
SEED_PRODUCTS_FILE = DATA_DIR / "seed_products.json"
PRODUCT_UPLOAD_DIR = STATIC_DIR / "uploads" / "products"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+\-()\s]{7,20}$")
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
ORDER_STATUSES = {
    "pending",
    "approved",
    "ready_for_shipment",
    "in_shipment",
    "delivered",
    "declined",
    "delayed",
}


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE_PATH", str(DEFAULT_DATABASE)),
        ADMIN_API_KEY=os.environ.get("ADMIN_API_KEY", ""),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-before-deployment"),
        ADMIN_DEFAULT_PASSWORD=os.environ.get("ADMIN_DEFAULT_PASSWORD", "admin123"),
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    @app.before_request
    def ensure_database():
        init_db()

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def add_local_dev_headers(response):
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Vary"] = "Origin"
        return response

    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/admin")
    def admin_page():
        return send_from_directory(FRONTEND_DIR, "admin.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "service": "luxetime-backend"})

    @app.get("/api/products")
    def list_products():
        rows = get_db().execute(
            """
            SELECT * FROM products
            WHERE active = 1
            ORDER BY source DESC, id ASC
            """
        ).fetchall()
        return jsonify([serialize_product(row) for row in rows])

    @app.post("/api/orders")
    def create_order():
        payload = request.get_json(silent=True) or {}
        errors = validate_order(payload)
        if errors:
            return jsonify({"errors": errors}), 400

        items = normalize_order_items(payload["items"])
        db = get_db()
        products = products_for_items(items)
        item_errors = validate_items_against_products(items, products)
        if item_errors:
            return jsonify({"errors": item_errors}), 400

        subtotal_cents = sum(int(round(products[item["product_id"]]["price"] * 100)) * item["qty"] for item in items)
        shipping_cents = 0 if subtotal_cents >= 50000 else 2500
        total_cents = subtotal_cents + shipping_cents
        tracking_id = generate_tracking_id()
        now = utc_now()

        cursor = db.execute(
            """
            INSERT INTO orders (
                tracking_id, customer_name, email, phone, shipping_address,
                total_cents, shipping_cents, card_last4, status, customer_note,
                admin_note, eta, stock_applied, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracking_id,
                payload["name"].strip(),
                payload["email"].strip().lower(),
                payload["phone"].strip(),
                payload["address"].strip(),
                total_cents,
                shipping_cents,
                "",
                "pending",
                str(payload.get("note", "")).strip(),
                "",
                "Waiting for admin approval.",
                0,
                now,
                now,
            ),
        )
        order_id = cursor.lastrowid

        for item in items:
            product = products[item["product_id"]]
            unit_price_cents = int(round(product["price"] * 100))
            db.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, product_name, brand, quantity,
                    unit_price_cents, line_total_cents
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["product_id"],
                    product["name"],
                    product["brand"],
                    item["qty"],
                    unit_price_cents,
                    unit_price_cents * item["qty"],
                ),
            )

        create_notification(
            "New order received",
            f"Tracking {tracking_id} is waiting for approval.",
            "order",
            tracking_id,
            commit=False,
        )
        db.commit()

        return jsonify({"tracking_id": tracking_id, "status": "pending", "total_cents": total_cents}), 201

    @app.get("/api/orders/track/<tracking_id>")
    def track_order(tracking_id):
        order = get_order_by_tracking(tracking_id)
        if order is None:
            return jsonify({"error": "Tracking ID not found"}), 404
        return jsonify(serialize_order(order, include_private=False))

    @app.post("/api/contact")
    def create_contact_message():
        payload = request.get_json(silent=True) or {}
        errors = validate_contact(payload)
        if errors:
            return jsonify({"errors": errors}), 400

        db = get_db()
        name = payload["name"].strip()
        email = payload["email"].strip().lower()
        message = payload["message"].strip()
        created_at = utc_now()
        cursor = db.execute(
            """
            INSERT INTO contact_messages (name, email, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                name,
                email,
                message,
                created_at,
            ),
        )
        create_notification(
            "New contact message",
            f"{name} ({email}) sent: {message}",
            "contact",
            str(cursor.lastrowid),
            commit=False,
        )
        db.commit()

        return jsonify({"id": cursor.lastrowid, "message": "Message received"}), 201

    @app.get("/api/admin/session")
    def admin_session():
        return jsonify({"authenticated": is_admin_session()})

    @app.post("/api/admin/login")
    def admin_login():
        payload = request.get_json(silent=True) or {}
        password = str(payload.get("password", ""))
        stored_hash = get_setting("admin_password_hash")
        if stored_hash and check_password_hash(stored_hash, password):
            session.clear()
            session["admin_authenticated"] = True
            return jsonify({"message": "Logged in"})
        return jsonify({"error": "Invalid password"}), 401

    @app.post("/api/admin/logout")
    def admin_logout():
        session.clear()
        return jsonify({"message": "Logged out"})

    @app.post("/api/admin/change-password")
    def change_admin_password():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        current_password = str(payload.get("current_password", ""))
        new_password = str(payload.get("new_password", ""))
        stored_hash = get_setting("admin_password_hash")

        if not stored_hash or not check_password_hash(stored_hash, current_password):
            return jsonify({"errors": {"current_password": "Current password is incorrect."}}), 400

        password_error = validate_new_password(new_password)
        if password_error:
            return jsonify({"errors": {"new_password": password_error}}), 400

        set_setting("admin_password_hash", generate_password_hash(new_password))
        return jsonify({"message": "Password updated"})

    @app.get("/api/admin/products")
    def admin_products():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        query = str(request.args.get("q", "")).strip().lower()
        category = str(request.args.get("category", "")).strip()
        active = str(request.args.get("active", "")).strip()

        sql = "SELECT * FROM products WHERE 1=1"
        params = []
        if query:
            sql += " AND (LOWER(name) LIKE ? OR LOWER(brand) LIKE ? OR LOWER(product_id) LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        if category:
            sql += " AND category = ?"
            params.append(category)
        if active in {"0", "1"}:
            sql += " AND active = ?"
            params.append(int(active))
        sql += " ORDER BY active DESC, category ASC, name ASC"

        rows = get_db().execute(sql, params).fetchall()
        return jsonify([serialize_product(row) for row in rows])

    @app.post("/api/admin/products")
    def create_product():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        errors = validate_product_form(request.form, request.files, require_image=True)
        if errors:
            return jsonify({"errors": errors}), 400

        image_paths = save_product_images(request.files.getlist("images"), request.form["name"])
        product = normalize_product_form(request.form, image_paths)
        insert_product(product)
        return jsonify(product), 201

    @app.patch("/api/admin/products/<product_id>")
    def update_product(product_id):
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        row = get_product(product_id)
        if row is None:
            return jsonify({"error": "Product not found"}), 404

        errors = validate_product_update(payload)
        if errors:
            return jsonify({"errors": errors}), 400

        existing = serialize_product(row)
        updated = {
            "name": str(payload.get("name", existing["name"])).strip(),
            "brand": str(payload.get("brand", existing["brand"])).strip(),
            "category": str(payload.get("category", existing["category"])).strip(),
            "price": float(payload.get("price", existing["price"])),
            "orig": nullable_float(payload.get("orig", existing["orig"])),
            "rating": float(payload.get("rating", existing["rating"])),
            "reviews": int(payload.get("reviews", existing["reviews"])),
            "desc": str(payload.get("desc", existing["desc"])).strip(),
            "colors": payload.get("colors", existing["colors"]),
            "specs": payload.get("specs", existing["specs"]),
            "stock": int(payload.get("stock", existing["stock"])),
            "active": 1 if bool(payload.get("active", existing["active"])) else 0,
        }
        now = utc_now()
        get_db().execute(
            """
            UPDATE products
            SET name = ?, brand = ?, category = ?, price = ?, orig = ?, rating = ?,
                reviews = ?, description = ?, colors_json = ?, specs_json = ?,
                stock = ?, active = ?, updated_at = ?
            WHERE product_id = ?
            """,
            (
                updated["name"],
                updated["brand"],
                updated["category"],
                updated["price"],
                updated["orig"],
                updated["rating"],
                updated["reviews"],
                updated["desc"],
                json.dumps(updated["colors"]),
                json.dumps(updated["specs"]),
                updated["stock"],
                updated["active"],
                now,
                product_id,
            ),
        )
        get_db().commit()
        return jsonify(serialize_product(get_product(product_id)))

    @app.delete("/api/admin/products/<product_id>")
    def remove_product(product_id):
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401
        if get_product(product_id) is None:
            return jsonify({"error": "Product not found"}), 404
        get_db().execute(
            "UPDATE products SET active = 0, updated_at = ? WHERE product_id = ?",
            (utc_now(), product_id),
        )
        get_db().commit()
        return jsonify({"message": "Product removed"})

    @app.get("/api/admin/orders")
    def list_orders():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        status = str(request.args.get("status", "")).strip()
        query = str(request.args.get("q", "")).strip().lower()
        sql = "SELECT * FROM orders WHERE 1=1"
        params = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if query:
            sql += " AND (LOWER(tracking_id) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(email) LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        sql += " ORDER BY id DESC"

        rows = get_db().execute(sql, params).fetchall()
        return jsonify([serialize_order(row, include_private=True) for row in rows])

    @app.patch("/api/admin/orders/<tracking_id>")
    def update_order_status(tracking_id):
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status", "")).strip()
        if status not in ORDER_STATUSES:
            return jsonify({"errors": {"status": "Choose a valid order status."}}), 400

        order = get_order_by_tracking(tracking_id)
        if order is None:
            return jsonify({"error": "Order not found"}), 404

        if status == "approved" and not order["stock_applied"]:
            stock_errors = apply_stock_for_order(order["id"])
            if stock_errors:
                return jsonify({"errors": stock_errors}), 400

        now = utc_now()
        get_db().execute(
            """
            UPDATE orders
            SET status = ?, admin_note = ?, eta = ?, updated_at = ?
            WHERE tracking_id = ?
            """,
            (
                status,
                str(payload.get("admin_note", order["admin_note"] or "")).strip(),
                str(payload.get("eta", order["eta"] or "")).strip(),
                now,
                tracking_id,
            ),
        )
        create_notification(
            "Order status updated",
            f"{tracking_id} is now {status.replace('_', ' ')}.",
            "order",
            tracking_id,
            commit=False,
        )
        get_db().commit()
        return jsonify(serialize_order(get_order_by_tracking(tracking_id), include_private=True))

    @app.get("/api/admin/notifications")
    def list_notifications():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401
        rows = get_db().execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT 50"
        ).fetchall()
        unread = get_db().execute("SELECT COUNT(*) AS c FROM notifications WHERE is_read = 0").fetchone()["c"]
        return jsonify({"unread": unread, "items": [serialize_notification(row) for row in rows]})

    @app.post("/api/admin/notifications/read")
    def mark_notifications_read():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401
        get_db().execute("UPDATE notifications SET is_read = 1")
        get_db().commit()
        return jsonify({"message": "Notifications marked as read"})

    @app.get("/api/admin/contact-messages")
    def list_contact_messages():
        if not admin_allowed(request):
            return jsonify({"error": "Unauthorized"}), 401
        rows = get_db().execute(
            """
            SELECT id, name, email, message, created_at
            FROM contact_messages
            ORDER BY id DESC
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    return app


def get_db():
    if "db" not in g:
        database_path = Path(current_app.config["DATABASE"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            brand TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            orig REAL,
            rating REAL NOT NULL DEFAULT 4.5,
            reviews INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL,
            colors_json TEXT NOT NULL,
            specs_json TEXT NOT NULL,
            image_path TEXT NOT NULL,
            images_json TEXT,
            stock INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_id TEXT UNIQUE,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            shipping_address TEXT NOT NULL,
            product_id TEXT,
            product_name TEXT,
            total_cents INTEGER NOT NULL,
            shipping_cents INTEGER NOT NULL DEFAULT 0,
            card_last4 TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            customer_note TEXT,
            admin_note TEXT,
            eta TEXT,
            stock_applied INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            brand TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price_cents INTEGER NOT NULL,
            line_total_cents INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT NOT NULL,
            ref TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    ensure_columns()
    if get_setting("admin_password_hash") is None:
        set_setting(
            "admin_password_hash",
            generate_password_hash(current_app.config["ADMIN_DEFAULT_PASSWORD"]),
            commit=False,
        )
    db.commit()
    seed_products()
    migrate_legacy_orders()


def ensure_columns():
    ensure_table_columns(
        "products",
        {
            "images_json": "TEXT",
            "stock": "INTEGER NOT NULL DEFAULT 0",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "source": "TEXT NOT NULL DEFAULT 'admin'",
            "updated_at": "TEXT",
        },
    )
    ensure_table_columns(
        "orders",
        {
            "tracking_id": "TEXT",
            "phone": "TEXT",
            "shipping_cents": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "customer_note": "TEXT",
            "admin_note": "TEXT",
            "eta": "TEXT",
            "stock_applied": "INTEGER NOT NULL DEFAULT 0",
            "updated_at": "TEXT",
        },
    )


def ensure_table_columns(table, columns):
    existing = {row["name"] for row in get_db().execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            get_db().execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def seed_products():
    if not SEED_PRODUCTS_FILE.exists():
        return
    products = json.loads(SEED_PRODUCTS_FILE.read_text(encoding="utf-8"))
    for product in products:
        exists = get_db().execute(
            "SELECT id FROM products WHERE product_id = ?",
            (product["id"],),
        ).fetchone()
        if exists:
            continue
        normalized = {
            "id": product["id"],
            "name": product["name"],
            "brand": product["brand"],
            "category": product["category"],
            "price": product["price"],
            "orig": product.get("orig"),
            "rating": product.get("rating", 4.5),
            "reviews": product.get("reviews", 0),
            "desc": product["desc"],
            "colors": product.get("colors") or ["#1A1A1A", "#B8975A"],
            "specs": product.get("specs") or {},
            "img": product["img"],
            "images": product.get("images") or [product["img"]],
            "stock": product.get("stock", 10),
            "active": 1,
            "source": "predefined",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        insert_product(normalized, commit=False)
    get_db().commit()


def migrate_legacy_orders():
    rows = get_db().execute(
        "SELECT * FROM orders WHERE tracking_id IS NULL OR tracking_id = ''"
    ).fetchall()
    for row in rows:
        tracking_id = generate_tracking_id()
        get_db().execute(
            """
            UPDATE orders
            SET tracking_id = ?, phone = COALESCE(phone, 'Not provided'),
                status = COALESCE(status, 'pending'),
                eta = COALESCE(eta, 'Waiting for admin approval.'),
                updated_at = COALESCE(updated_at, created_at)
            WHERE id = ?
            """,
            (tracking_id, row["id"]),
        )
        item_count = get_db().execute(
            "SELECT COUNT(*) AS c FROM order_items WHERE order_id = ?",
            (row["id"],),
        ).fetchone()["c"]
        if item_count == 0 and row["product_id"]:
            unit_price_cents = row["total_cents"] or 0
            get_db().execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, product_name, brand, quantity,
                    unit_price_cents, line_total_cents
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["product_id"],
                    row["product_name"] or "Legacy product",
                    "Legacy",
                    1,
                    unit_price_cents,
                    unit_price_cents,
                ),
            )
    if rows:
        get_db().commit()


def insert_product(product, commit=True):
    get_db().execute(
        """
        INSERT INTO products (
            product_id, name, brand, category, price, orig, rating,
            reviews, description, colors_json, specs_json, image_path,
            images_json, stock, active, source, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product["id"],
            product["name"],
            product["brand"],
            product["category"],
            product["price"],
            product["orig"],
            product["rating"],
            product["reviews"],
            product["desc"],
            json.dumps(product["colors"]),
            json.dumps(product["specs"]),
            product["img"],
            json.dumps(product.get("images") or [product["img"]]),
            product["stock"],
            1 if product.get("active", True) else 0,
            product.get("source", "admin"),
            product["created_at"],
            product.get("updated_at") or product["created_at"],
        ),
    )
    if commit:
        get_db().commit()


def validate_order(payload):
    errors = {}
    required_fields = {
        "name": "Full name is required.",
        "email": "A valid email address is required.",
        "phone": "Phone number is required.",
        "address": "Delivery address is required.",
    }
    for field, message in required_fields.items():
        if not str(payload.get(field, "")).strip():
            errors[field] = message

    email = str(payload.get("email", "")).strip()
    if email and not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."

    phone = str(payload.get("phone", "")).strip()
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "Enter a valid phone number."

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors["items"] = "Add at least one product to the order."
    else:
        for index, item in enumerate(items):
            if not str(item.get("product_id", "")).strip():
                errors[f"items[{index}]"] = "Product ID is required."
            try:
                qty = int(item.get("qty", 0))
                if qty <= 0:
                    errors[f"items[{index}].qty"] = "Quantity must be at least 1."
            except (TypeError, ValueError):
                errors[f"items[{index}].qty"] = "Quantity must be a whole number."

    return errors


def normalize_order_items(items):
    merged = {}
    for item in items:
        product_id = str(item["product_id"]).strip()
        merged[product_id] = merged.get(product_id, 0) + int(item["qty"])
    return [{"product_id": product_id, "qty": qty} for product_id, qty in merged.items()]


def products_for_items(items):
    if not items:
        return {}
    placeholders = ",".join("?" for _ in items)
    rows = get_db().execute(
        f"SELECT * FROM products WHERE active = 1 AND product_id IN ({placeholders})",
        [item["product_id"] for item in items],
    ).fetchall()
    return {row["product_id"]: row for row in rows}


def validate_items_against_products(items, products):
    errors = {}
    for item in items:
        product = products.get(item["product_id"])
        if product is None:
            errors[item["product_id"]] = "Product is unavailable."
        elif product["stock"] < item["qty"]:
            errors[item["product_id"]] = f"Only {product['stock']} item(s) in stock."
    return errors


def apply_stock_for_order(order_id):
    db = get_db()
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    errors = {}
    for item in items:
        product = get_product(item["product_id"])
        if product is None or not product["active"]:
            errors[item["product_id"]] = "Product is no longer available."
        elif product["stock"] < item["quantity"]:
            errors[item["product_id"]] = f"Only {product['stock']} item(s) in stock."
    if errors:
        return errors

    for item in items:
        db.execute(
            "UPDATE products SET stock = stock - ?, updated_at = ? WHERE product_id = ?",
            (item["quantity"], utc_now(), item["product_id"]),
        )
    db.execute("UPDATE orders SET stock_applied = 1 WHERE id = ?", (order_id,))
    return {}


def validate_contact(payload):
    errors = {}
    for field in ("name", "email", "message"):
        if not str(payload.get(field, "")).strip():
            errors[field] = f"{field.replace('_', ' ').title()} is required."

    email = str(payload.get("email", "")).strip()
    if email and not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."

    message = str(payload.get("message", "")).strip()
    if message and len(message) < 10:
        errors["message"] = "Message must be at least 10 characters."

    return errors


def validate_new_password(password):
    if len(password) < 10:
        return "Use at least 10 characters."
    if not re.search(r"[A-Z]", password):
        return "Add at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Add at least one lowercase letter."
    if not re.search(r"\d", password):
        return "Add at least one number."
    return None


def validate_product_form(form, files, require_image):
    errors = {}
    for field in ("name", "brand", "category", "price", "description", "stock"):
        if not str(form.get(field, "")).strip():
            errors[field] = f"{field.replace('_', ' ').title()} is required."

    category = str(form.get("category", "")).strip()
    if category and category not in {"luxury", "sports", "casual", "smart"}:
        errors["category"] = "Choose a valid category."

    for field in ("price", "orig", "rating"):
        value = str(form.get(field, "")).strip()
        if value:
            try:
                parsed = float(value)
                if parsed < 0:
                    errors[field] = f"{field.title()} cannot be negative."
            except ValueError:
                errors[field] = f"{field.title()} must be a number."

    rating = str(form.get("rating", "")).strip()
    if rating and "rating" not in errors and float(rating) > 5:
        errors["rating"] = "Rating cannot be above 5."

    for field in ("reviews", "stock"):
        value = str(form.get(field, "")).strip()
        if value:
            try:
                if int(value) < 0:
                    errors[field] = f"{field.title()} cannot be negative."
            except ValueError:
                errors[field] = f"{field.title()} must be a whole number."

    images = files.getlist("images")
    if require_image and not images:
        errors["images"] = "At least one product image is required."
    for image in images:
        if image.filename and not allowed_image(image.filename):
            errors["images"] = "Upload JPG, PNG, WebP, or GIF images."
    return errors


def validate_product_update(payload):
    errors = {}
    category = str(payload.get("category", "")).strip()
    if category and category not in {"luxury", "sports", "casual", "smart"}:
        errors["category"] = "Choose a valid category."
    for field in ("price", "orig", "rating"):
        if field in payload and payload[field] not in (None, ""):
            try:
                if float(payload[field]) < 0:
                    errors[field] = f"{field.title()} cannot be negative."
            except (TypeError, ValueError):
                errors[field] = f"{field.title()} must be a number."
    if "rating" in payload and "rating" not in errors and float(payload["rating"]) > 5:
        errors["rating"] = "Rating cannot be above 5."
    for field in ("reviews", "stock"):
        if field in payload:
            try:
                if int(payload[field]) < 0:
                    errors[field] = f"{field.title()} cannot be negative."
            except (TypeError, ValueError):
                errors[field] = f"{field.title()} must be a whole number."
    return errors


def normalize_product_form(form, image_paths):
    name = form["name"].strip()
    category = form["category"].strip()
    product_id = slugify(f"{category}-{name}-{int(time.time() * 1000)}")
    now = utc_now()
    return {
        "id": product_id,
        "name": name,
        "brand": form["brand"].strip(),
        "category": category,
        "price": float(form["price"]),
        "orig": nullable_float(form.get("orig")),
        "rating": float(form.get("rating") or 4.5),
        "reviews": int(form.get("reviews") or 0),
        "desc": form["description"].strip(),
        "colors": parse_colors(form.get("colors", "")),
        "specs": parse_specs(form.get("specs", "")),
        "img": image_paths[0],
        "images": image_paths,
        "stock": int(form.get("stock") or 0),
        "active": True,
        "source": "admin",
        "created_at": now,
        "updated_at": now,
    }


def parse_colors(value):
    colors = [item.strip() for item in str(value).split(",") if item.strip()]
    return colors or ["#1A1A1A", "#B8975A"]


def parse_specs(value):
    specs = {}
    for line in str(value).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key and val:
                specs[key] = val
    return specs or {"Movement": "To be specified", "Case": "To be specified"}


def save_product_images(images, product_name):
    PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, image in enumerate(images):
        if not image.filename:
            continue
        original = secure_filename(image.filename)
        ext = original.rsplit(".", 1)[-1].lower()
        filename = f"{slugify(product_name)}-{int(time.time() * 1000)}-{index + 1}.{ext}"
        image.save(PRODUCT_UPLOAD_DIR / filename)
        paths.append(f"/static/uploads/products/{filename}")
    return paths


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_IMAGE_EXTENSIONS


def get_product(product_id):
    return get_db().execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()


def get_order_by_tracking(tracking_id):
    return get_db().execute("SELECT * FROM orders WHERE tracking_id = ?", (tracking_id,)).fetchone()


def serialize_product(row):
    images = json.loads(row["images_json"]) if row["images_json"] else [row["image_path"]]
    return {
        "id": row["product_id"],
        "name": row["name"],
        "brand": row["brand"],
        "category": row["category"],
        "price": row["price"],
        "orig": row["orig"],
        "rating": row["rating"],
        "reviews": row["reviews"],
        "desc": row["description"],
        "colors": json.loads(row["colors_json"]),
        "specs": json.loads(row["specs_json"]),
        "img": row["image_path"],
        "images": images,
        "stock": row["stock"],
        "active": bool(row["active"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def serialize_order(row, include_private):
    items = get_db().execute(
        "SELECT product_id, product_name, brand, quantity, unit_price_cents, line_total_cents FROM order_items WHERE order_id = ?",
        (row["id"],),
    ).fetchall()
    data = {
        "tracking_id": row["tracking_id"],
        "status": row["status"],
        "status_label": row["status"].replace("_", " ").title(),
        "eta": row["eta"],
        "admin_note": row["admin_note"],
        "total_cents": row["total_cents"],
        "shipping_cents": row["shipping_cents"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "items": [dict(item) for item in items],
    }
    if include_private:
        data.update(
            {
                "customer_name": row["customer_name"],
                "email": row["email"],
                "phone": row["phone"],
                "shipping_address": row["shipping_address"],
                "customer_note": row["customer_note"],
                "stock_applied": bool(row["stock_applied"]),
            }
        )
    return data


def serialize_notification(row):
    data = dict(row)
    data["details"] = None
    if row["type"] == "contact" and row["ref"]:
        contact = get_db().execute(
            "SELECT id, name, email, message, created_at FROM contact_messages WHERE id = ?",
            (row["ref"],),
        ).fetchone()
        if contact:
            data["details"] = dict(contact)
    elif row["type"] == "order" and row["ref"]:
        order = get_order_by_tracking(row["ref"])
        if order:
            data["details"] = {
                "tracking_id": order["tracking_id"],
                "customer_name": order["customer_name"],
                "email": order["email"],
                "phone": order["phone"],
                "status": order["status"],
            }
    return data


def create_notification(title, message, type_, ref, commit=True):
    get_db().execute(
        """
        INSERT INTO notifications (title, message, type, ref, is_read, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (title, message, type_, ref, utc_now()),
    )
    if commit:
        get_db().commit()


def admin_allowed(req):
    configured_key = current_app.config.get("ADMIN_API_KEY", "")
    key_allowed = bool(configured_key) and req.headers.get("X-Admin-Key") == configured_key
    return is_admin_session() or key_allowed


def is_admin_session():
    return bool(session.get("admin_authenticated"))


def get_setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value, commit=True):
    get_db().execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    if commit:
        get_db().commit()


def generate_tracking_id():
    return f"LXT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def slugify(value):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "product"


def nullable_float(value):
    if value in (None, ""):
        return None
    return float(value)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
