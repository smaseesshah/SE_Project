# LUXE Time

Luxury watch e-commerce semester project with:

- Customer storefront
- Cart and order placement
- Tracking ID lookup
- Contact form
- Admin dashboard
- Product management
- Stock management
- Order approval/status workflow
- Admin notifications

## Project Structure

```text
backend/              Flask backend
frontend/             Customer and admin HTML pages
data/                 Seed product data
static/               Local product images
wsgi.py               App runner
requirements.txt      Python dependencies
README.md             Setup guide
```

## Requirements

- Python 3.10+
- pip

## Run Locally

Create virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```bash
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the project:

```bash
python wsgi.py
```

Open storefront:

```text
http://127.0.0.1:5000
```

Open admin:

```text
http://127.0.0.1:5000/admin
```

Default admin password:

```text
admin123
```

Change the password from the admin security section.

## Important Notes

- Do not open `frontend/index.html` directly. Run `python wsgi.py` and open `http://127.0.0.1:5000`.
- The database is created automatically inside `instance/`.
- Uploaded admin product images are saved inside `static/uploads/`.
- Predefined product data comes from `data/seed_products.json`.
- Local sample images are inside `static/images/seed/`.

## Main Files

- `backend/app.py`: backend routes, database, admin auth, products, orders, tracking, notifications.
- `frontend/index.html`: customer storefront.
- `frontend/admin.html`: admin dashboard.
- `data/seed_products.json`: predefined product catalog.
- `wsgi.py`: starts the Flask app.

## Simple Deployment

Install production dependencies:

```bash
pip install -r requirements.txt
```

Run with Gunicorn on hosting platforms:

```bash
gunicorn wsgi:app
```

For a semester demo, local SQLite is enough. For real production, use PostgreSQL and cloud image storage.
