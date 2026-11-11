"""
FreshBasket app description:

A small Flask app deployed on AWS Elastic Beanstalk. Every request follows the
full architecture path:

    Browser -> Application Load Balancer -> EC2 instance (Auto Scaling Group) -> RDS MySQL

Growers list produce (WRITE to RDS); everyone sees the live listings (READ from RDS).
Each page also shows which EC2 instance / Availability Zone served it, so refreshing
proves the Load Balancer is spreading traffic across the Auto Scaling Group.

Database settings come from the RDS_* environment variables that Elastic Beanstalk
injects automatically when the database is created as part of the environment.
"""

import os
import urllib.request
from datetime import datetime, timezone

import pymysql
from flask import Flask, redirect, render_template_string, request, url_for

application = Flask(__name__)  # Elastic Beanstalk's Python platform looks for "application"
app = application

DB_CONFIG = {
    "host": os.environ.get("RDS_HOSTNAME", "localhost"),
    "port": int(os.environ.get("RDS_PORT", "3306")),
    "user": os.environ.get("RDS_USERNAME", "admin"),
    "password": os.environ.get("RDS_PASSWORD", ""),
    "database": os.environ.get("RDS_DB_NAME", "ebdb"),
    "connect_timeout": 5,
    "read_timeout": 10,
    "write_timeout": 10,
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": True,
}

CATEGORIES = ["Vegetables", "Fruit", "Herbs", "Eggs & Dairy", "Pantry"]
MAX_NAME = 80

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS produce_listings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    grower_name VARCHAR(80) NOT NULL,
    produce_name VARCHAR(80) NOT NULL,
    category VARCHAR(40) NOT NULL,
    price_aud DECIMAL(8, 2) NOT NULL,
    quantity INT NOT NULL,
    served_by_instance VARCHAR(40) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SEED_ROWS = [
    ("Hawkesbury Greens", "Baby spinach (250 g)", "Vegetables", 4.50, 40),
    ("Blue Mountains Orchard", "Pink Lady apples (1 kg)", "Fruit", 6.90, 25),
    ("Camden Free Range", "Free-range eggs (dozen)", "Eggs & Dairy", 8.00, 30),
]

_table_ready = False


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def ensure_table():
    """Create the table (and a few seed rows) once per worker process."""
    global _table_ready
    if _table_ready:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Several instances boot at once behind the ASG; a named lock stops
            # them all seeding the sample rows at the same time.
            cur.execute("SELECT GET_LOCK('freshbasket_init', 10) AS got")
            try:
                cur.execute(CREATE_TABLE_SQL)
                cur.execute("SELECT COUNT(*) AS n FROM produce_listings")
                if cur.fetchone()["n"] == 0:
                    cur.executemany(
                        "INSERT INTO produce_listings "
                        "(grower_name, produce_name, category, price_aud, quantity, served_by_instance) "
                        "VALUES (%s, %s, %s, %s, %s, 'seed')",
                        SEED_ROWS,
                    )
            finally:
                cur.execute("SELECT RELEASE_LOCK('freshbasket_init')")
    finally:
        conn.close()
    _table_ready = True


# ---------------------------------------------------------------------------
# EC2 instance metadata (IMDSv2) - shows which instance served the request
# ---------------------------------------------------------------------------
_metadata_cache = {}


def _imds(path):
    if path in _metadata_cache:
        return _metadata_cache[path]
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
        )
        token = urllib.request.urlopen(token_req, timeout=1).read().decode()  # noqa: S310
        data_req = urllib.request.Request(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        value = urllib.request.urlopen(data_req, timeout=1).read().decode()  # noqa: S310
    except Exception:  # noqa: BLE001 - not on EC2 (e.g. local testing)
        value = "local"
    _metadata_cache[path] = value
    return value


def instance_info():
    return {
        "instance_id": _imds("instance-id"),
        "az": _imds("placement/availability-zone"),
        "private_ip": _imds("local-ipv4"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FreshBasket - Local Growers Marketplace</title>
<style>
  :root { --green:#2f6b3a; --leaf:#e8f2e3; --ink:#1f2a22; --muted:#5f6b62; --line:#d6e2d2; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color:var(--ink); background:#f7faf5; }
  header { background:var(--green); color:#fff; padding:20px 16px; }
  header h1 { margin:0; font-size:1.6rem; }
  header p { margin:4px 0 0; opacity:.9; }
  main { max-width:960px; margin:0 auto; padding:16px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .infra { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:8px; background:var(--leaf); }
  .infra div { font-size:.9rem; }
  .infra strong { display:block; font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .ok { color:#1d7a35; font-weight:600; } .bad { color:#a12d2d; font-weight:600; }
  form { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; align-items:end; }
  label { font-size:.85rem; color:var(--muted); display:block; margin-bottom:4px; }
  input, select { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; font-size:.95rem; }
  button { padding:10px 14px; background:var(--green); color:#fff; border:0; border-radius:6px; font-size:.95rem; cursor:pointer; }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); }
  th { background:var(--leaf); }
  .tablewrap { overflow-x:auto; }
  .flash { padding:10px; border-radius:6px; margin-bottom:12px; }
  .flash.success { background:#e3f4e6; } .flash.error { background:#fbe5e5; }
  footer { text-align:center; color:var(--muted); font-size:.8rem; padding:16px; }
</style>
</head>
<body>
<header>
  <h1>FreshBasket</h1>
  <p>Connecting local Sydney growers with home cooks</p>
</header>
<main>
  <section class="card infra" aria-label="Request path">
    <div><strong>Served by EC2 instance</strong>{{ info.instance_id }}</div>
    <div><strong>Availability Zone</strong>{{ info.az }}</div>
    <div><strong>Instance private IP</strong>{{ info.private_ip }}</div>
    <div><strong>RDS MySQL</strong>
      {% if db_ok %}<span class="ok">Connected</span> ({{ db_version }}){% else %}<span class="bad">Not connected</span>{% endif %}</div>
    <div><strong>Listings in database</strong>{{ listings|length }}</div>
    <div><strong>Page generated (UTC)</strong>{{ now }}</div>
  </section>

  {% if message %}<div class="flash {{ message_kind }}">{{ message }}</div>{% endif %}
  {% if db_error %}<div class="flash error">Database error: {{ db_error }}</div>{% endif %}

  <section class="card">
    <h2>List your produce (writes to RDS)</h2>
    <form method="post" action="{{ url_for('add_listing') }}">
      <div><label for="grower">Grower / farm name</label>
        <input id="grower" name="grower_name" maxlength="80" required></div>
      <div><label for="produce">Produce</label>
        <input id="produce" name="produce_name" maxlength="80" required placeholder="e.g. Heirloom tomatoes (1 kg)"></div>
      <div><label for="category">Category</label>
        <select id="category" name="category">{% for c in categories %}<option>{{ c }}</option>{% endfor %}</select></div>
      <div><label for="price">Price (AUD)</label>
        <input id="price" name="price_aud" type="number" min="0.01" max="9999" step="0.01" required></div>
      <div><label for="qty">Quantity available</label>
        <input id="qty" name="quantity" type="number" min="1" max="100000" step="1" required></div>
      <div><button type="submit">Add listing</button></div>
    </form>
  </section>

  <section class="card">
    <h2>Fresh listings (read from RDS)</h2>
    <div class="tablewrap">
    <table>
      <thead><tr><th>#</th><th>Grower</th><th>Produce</th><th>Category</th><th>Price</th><th>Qty</th><th>Written by instance</th><th>Listed (UTC)</th></tr></thead>
      <tbody>
      {% for row in listings %}
        <tr><td>{{ row.id }}</td><td>{{ row.grower_name }}</td><td>{{ row.produce_name }}</td><td>{{ row.category }}</td>
            <td>${{ '%.2f'|format(row.price_aud) }}</td><td>{{ row.quantity }}</td><td>{{ row.served_by_instance }}</td>
            <td>{{ row.created_at }}</td></tr>
      {% else %}
        <tr><td colspan="8">No listings yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </section>
</main>
<footer>Refresh the page to watch the Load Balancer route you to different instances. Data is shared because every instance uses the same RDS database.</footer>
</body>
</html>"""


def _render(message=None, message_kind="success", status=200):
    listings, db_ok, db_version, db_error = [], False, "", None
    try:
        ensure_table()
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                db_version = "MySQL " + cur.fetchone()["v"]
                cur.execute(
                    "SELECT id, grower_name, produce_name, category, price_aud, quantity, "
                    "served_by_instance, created_at FROM produce_listings ORDER BY id DESC LIMIT 100"
                )
                listings = cur.fetchall()
            db_ok = True
        finally:
            conn.close()
    except pymysql.MySQLError as exc:
        db_error = f"{type(exc).__name__}: {exc.args[-1] if exc.args else exc}"
        status = 503
    return render_template_string(
        PAGE,
        info=instance_info(),
        listings=listings,
        db_ok=db_ok,
        db_version=db_version,
        db_error=db_error,
        categories=CATEGORIES,
        message=message,
        message_kind=message_kind,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    ), status


@application.get("/")
def index():
    added = request.args.get("added")
    msg = f"Listing #{added} saved to RDS." if added and added.isdigit() else None
    return _render(message=msg)


@application.post("/listings")
def add_listing():
    form = request.form
    grower = (form.get("grower_name") or "").strip()[:MAX_NAME]
    produce = (form.get("produce_name") or "").strip()[:MAX_NAME]
    category = form.get("category") if form.get("category") in CATEGORIES else None
    try:
        price = round(float(form.get("price_aud", "")), 2)
        quantity = int(form.get("quantity", ""))
    except ValueError:
        price, quantity = None, None

    if not grower or not produce or category is None or price is None or not (0 < price < 10000) \
            or quantity is None or not (0 < quantity <= 100000):
        return _render("Please fill in every field with valid values.", "error", 400)

    try:
        ensure_table()
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO produce_listings "
                    "(grower_name, produce_name, category, price_aud, quantity, served_by_instance) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (grower, produce, category, price, quantity, instance_info()["instance_id"]),
                )
                new_id = cur.lastrowid
        finally:
            conn.close()
    except pymysql.MySQLError as exc:
        return _render(f"Could not save listing: {type(exc).__name__}", "error", 503)

    # Post/Redirect/Get so a browser refresh does not resubmit the form
    return redirect(url_for("index", added=new_id))


@application.get("/health")
def health():
    """Load Balancer health check. Deliberately does NOT touch the database:
    an RDS blip should not make the ALB mark every instance unhealthy and the
    Auto Scaling Group replace healthy instances."""
    return {"status": "ok", "instance": instance_info()["instance_id"]}, 200


@application.get("/db-check")
def db_check():
    """Explicit end-to-end check: this instance can reach RDS and read from it."""
    try:
        ensure_table()
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM produce_listings")
                count = cur.fetchone()["n"]
        finally:
            conn.close()
        return {"status": "ok", "database": DB_CONFIG["host"], "listing_count": count,
                "instance": instance_info()}, 200
    except pymysql.MySQLError as exc:
        return {"status": "error", "error": type(exc).__name__, "instance": instance_info()}, 503


if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))  # noqa: S104
