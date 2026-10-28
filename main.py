"""
Expense Tracker - remote MCP server (FastMCP + SQLite)

Run locally (stdio):      python main.py
Run as remote HTTP:       python main.py --http
MCP endpoint (HTTP):      http://<host>:<PORT>/mcp
Health check (no auth):   http://<host>:<PORT>/health

Environment variables:
  PORT        Port to listen on (default 8000; most hosts set this for you)
  DB_PATH     SQLite file path (default ./expenses.db). Point at a mounted volume in production.
  AUTH_TOKEN  Bearer token clients must send: "Authorization: Bearer <token>"
"""
import hmac
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "expenses.db"))
CATEGORIES_PATH = os.environ.get("CATEGORIES_PATH", os.path.join(BASE_DIR, "categories.json"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

DEFAULT_CATEGORIES = [
    "Food & Dining",
    "Transportation",
    "Shopping",
    "Entertainment",
    "Bills & Utilities",
    "Healthcare",
    "Travel",
    "Education",
    "Business",
    "Other",
]

mcp = FastMCP("ExpenseTracker")


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    """Open a connection, commit on success, roll back on error, always close."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)")


def load_categories() -> list[str]:
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            cats = data.get("categories")
            if isinstance(cats, list) and cats:
                return cats
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return DEFAULT_CATEGORIES


def validate_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD.")


def normalize_category(value: str) -> str:
    """Case-insensitive match against the allowed categories."""
    allowed = load_categories()
    for cat in allowed:
        if cat.lower() == value.strip().lower():
            return cat
    raise ValueError(f"Unknown category '{value}'. Allowed: {', '.join(allowed)}")


init_db()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = "",
) -> dict:
    """Add a new expense. date must be YYYY-MM-DD; amount must be greater than 0."""
    date = validate_date(date)
    if amount <= 0:
        raise ValueError("Amount must be greater than 0.")
    category = normalize_category(category)

    with db() as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
            (date, amount, category, subcategory, note),
        )
        return {"status": "success", "id": cur.lastrowid, "message": "Expense added successfully"}


@mcp.tool()
def list_expenses(start_date: str, end_date: str) -> list[dict]:
    """List expenses within an inclusive date range (YYYY-MM-DD), newest first."""
    start_date, end_date = validate_date(start_date), validate_date(end_date)
    with db() as c:
        rows = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY date DESC, id DESC
            """,
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


@mcp.tool()
def update_expense(
    expense_id: int,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Update fields of an existing expense by id. Only provided fields are changed."""
    fields, params = [], []

    if date is not None:
        fields.append("date = ?")
        params.append(validate_date(date))
    if amount is not None:
        if amount <= 0:
            raise ValueError("Amount must be greater than 0.")
        fields.append("amount = ?")
        params.append(amount)
    if category is not None:
        fields.append("category = ?")
        params.append(normalize_category(category))
    if subcategory is not None:
        fields.append("subcategory = ?")
        params.append(subcategory)
    if note is not None:
        fields.append("note = ?")
        params.append(note)

    if not fields:
        raise ValueError("No fields provided to update.")

    params.append(expense_id)
    with db() as c:
        cur = c.execute(f"UPDATE expenses SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        return {"status": "success", "id": expense_id, "message": "Expense updated"}


@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    """Delete an expense by id."""
    with db() as c:
        cur = c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        return {"status": "success", "id": expense_id, "message": "Expense deleted"}


@mcp.tool()
def summarize(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
) -> list[dict]:
    """Summarize expenses by category within an inclusive date range (YYYY-MM-DD)."""
    start_date, end_date = validate_date(start_date), validate_date(end_date)

    query = """
        SELECT category, ROUND(SUM(amount), 2) AS total_amount, COUNT(*) AS count
        FROM expenses
        WHERE date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if category:
        query += " AND category = ?"
        params.append(normalize_category(category))

    query += " GROUP BY category ORDER BY total_amount DESC"

    with db() as c:
        return [dict(r) for r in c.execute(query, params).fetchall()]


@mcp.tool()
def monthly_total(year: int, month: int) -> dict:
    """Get the total spent in a given month."""
    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    prefix = f"{year:04d}-{month:02d}-%"
    with db() as c:
        row = c.execute(
            "SELECT ROUND(COALESCE(SUM(amount), 0), 2) AS total, COUNT(*) AS count "
            "FROM expenses WHERE date LIKE ?",
            (prefix,),
        ).fetchone()
        return {"year": year, "month": month, "total": row["total"], "count": row["count"]}


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
@mcp.resource("expense:///categories", mime_type="application/json")
def categories() -> str:
    """Return the allowed expense categories."""
    return json.dumps({"categories": load_categories()}, indent=2)


# --------------------------------------------------------------------------- #
# Health check (unauthenticated, for hosting platforms)
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------- #
# Bearer-token auth (ASGI middleware)
# --------------------------------------------------------------------------- #
class BearerAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode()
            expected = f"Bearer {AUTH_TOKEN}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app():
    app = mcp.http_app(path="/mcp")
    if AUTH_TOKEN:
        app.add_middleware(BearerAuthMiddleware)
        print("Bearer token authentication enabled.")
    else:
        print("[WARNING] AUTH_TOKEN not set. Running HTTP server without authentication (development mode).")
    return app


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def main():
    if "--http" in sys.argv:
        import uvicorn

        port = int(os.environ.get("PORT", 8000))
        uvicorn.run(build_http_app(), host="0.0.0.0", port=port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()