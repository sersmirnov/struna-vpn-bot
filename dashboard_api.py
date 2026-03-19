"""StrunaVPN Dashboard API v2 — stats + payments log."""

import asyncio
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
import asyncpg
import httpx

DB_URL = os.getenv("DATABASE_URL", "postgresql://strunavpn:strunadb2026@localhost:5432/strunavpn")
MARZBAN_URL = os.getenv("MARZBAN_BASE_URL", "http://localhost:8000")
MARZBAN_USER = os.getenv("MARZBAN_USERNAME", "admin")
MARZBAN_PASS = os.getenv("MARZBAN_PASSWORD", "")

# Read password from bot .env if not set
if not MARZBAN_PASS:
    try:
        with open("/opt/strunavpn/.env") as f:
            for line in f:
                if line.startswith("MARZBAN_PASSWORD="):
                    MARZBAN_PASS = line.strip().split("=", 1)[1]
    except Exception:
        pass

SERVER_COST_EUR = 7.72
SERVER_TRAFFIC_TB = 20
EUR_RUB = 105

# Simple auth token (change this!)
API_TOKEN = os.getenv("DASHBOARD_TOKEN", "struna2026")


async def get_pool():
    return await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)


async def get_stats():
    pool = await get_pool()
    try:
        # Basic counts
        total = await pool.fetchval("SELECT COUNT(*) FROM users")
        free = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'free' OR plan_expires_at IS NULL OR plan_expires_at < NOW()")
        basic = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'basic' AND plan_expires_at > NOW()")
        pro = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'pro' AND plan_expires_at > NOW()")

        new_today = await pool.fetchval("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'")
        new_7d = await pool.fetchval("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'")

        keys = await pool.fetchval("SELECT COUNT(*) FROM users WHERE marzban_username IS NOT NULL")

        # Payments — all time totals
        total_stars = await pool.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'stars'"
        )
        total_rub = await pool.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'yoomoney'"
        )
        total_payments = await pool.fetchval(
            "SELECT COUNT(*) FROM payments WHERE status = 'completed'"
        )

        # This month
        month_stars = await pool.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM payments 
               WHERE status = 'completed' AND method = 'stars'
               AND completed_at >= DATE_TRUNC('month', NOW())"""
        )
        month_rub = await pool.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM payments 
               WHERE status = 'completed' AND method = 'yoomoney'
               AND completed_at >= DATE_TRUNC('month', NOW())"""
        )

        referrals = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        tickets = await pool.fetchval("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")

        # Daily signups (30 days)
        signups_rows = await pool.fetch("""
            SELECT DATE(created_at) as d, COUNT(*) as c
            FROM users
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY d
        """)

        # Daily revenue (30 days)
        revenue_rows = await pool.fetch("""
            SELECT DATE(completed_at) as d,
                   COUNT(*) as cnt,
                   SUM(CASE WHEN method = 'stars' THEN amount ELSE 0 END) as stars,
                   SUM(CASE WHEN method = 'yoomoney' THEN amount ELSE 0 END) as rub
            FROM payments
            WHERE status = 'completed' AND completed_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(completed_at)
            ORDER BY d
        """)

        # Users list (last 200)
        users_list = await pool.fetch("""
            SELECT u.telegram_id, u.username, u.plan, u.traffic_used, u.traffic_limit,
                   u.plan_expires_at, u.marzban_username, u.referral_count, u.created_at,
                   u.devices_limit
            FROM users u
            ORDER BY u.created_at DESC
            LIMIT 200
        """)

        # Get Marzban online count
        online = 0
        total_traffic_bytes = 0
        try:
            async with httpx.AsyncClient(base_url=MARZBAN_URL, timeout=10) as client:
                resp = await client.post("/api/admin/token", data={"username": MARZBAN_USER, "password": MARZBAN_PASS})
                token = resp.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}
                sys_resp = await client.get("/api/system", headers=headers)
                if sys_resp.status_code == 200:
                    sys_data = sys_resp.json()
                    online = sys_data.get("users_active", 0)
                    total_traffic_bytes = sys_data.get("incoming_bandwidth", 0) + sys_data.get("outgoing_bandwidth", 0)
        except Exception as e:
            print(f"Marzban API error: {e}")

        traffic_gb = round(total_traffic_bytes / (1024**3), 1) if total_traffic_bytes else 0

        # Format signups (30 days)
        today = datetime.now(timezone.utc).date()
        signups_map = {row["d"]: row["c"] for row in signups_rows}
        signups_formatted = []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            signups_formatted.append({"date": d.isoformat(), "label": d.strftime("%d.%m"), "count": signups_map.get(d, 0)})

        # Format revenue (30 days)
        revenue_map = {row["d"]: row for row in revenue_rows}
        revenue_formatted = []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            row = revenue_map.get(d)
            revenue_formatted.append({
                "date": d.isoformat(),
                "label": d.strftime("%d.%m"),
                "count": int(row["cnt"]) if row else 0,
                "stars": int(row["stars"]) if row else 0,
                "rub": int(row["rub"]) if row else 0,
            })

        # Format users
        now = datetime.now(timezone.utc)
        users_formatted = []
        for u in users_list:
            exp = u["plan_expires_at"]
            if exp and exp > now:
                status = "active"
            elif exp and exp <= now:
                status = "expired"
            else:
                status = "new"

            used_gb = round(u["traffic_used"] / (1024**3), 2) if u["traffic_used"] else 0
            lim_gb = round(u["traffic_limit"] / (1024**3), 1) if u["traffic_limit"] else 0

            users_formatted.append({
                "telegram_id": u["telegram_id"],
                "username": u["username"],
                "plan": u["plan"] or "free",
                "traffic_gb": used_gb,
                "traffic_limit_gb": lim_gb,
                "expires": exp.isoformat() if exp else None,
                "status": status,
                "referral_count": u["referral_count"] or 0,
                "devices_limit": u["devices_limit"] or 1,
                "marzban_username": u["marzban_username"],
                "created_at": u["created_at"].isoformat() if u["created_at"] else None,
            })

        # Costs
        server_rub = SERVER_COST_EUR * EUR_RUB
        gb_cost_rub = server_rub / (SERVER_TRAFFIC_TB * 1024)
        paid = int(basic) + int(pro)
        total_rev_rub = int(total_stars) * 1.3 + int(total_rub)

        return {
            "stats": {
                "total_users": int(total),
                "free_users": int(free),
                "basic_users": int(basic),
                "pro_users": int(pro),
                "paid_users": paid,
                "keys_issued": int(keys),
                "online_now": online,
                "new_today": int(new_today),
                "new_7d": int(new_7d),
                "total_stars": int(total_stars),
                "total_rub": int(total_rub),
                "total_revenue_rub": round(total_rev_rub),
                "month_stars": int(month_stars),
                "month_rub": int(month_rub),
                "total_payments": int(total_payments),
                "traffic_gb": traffic_gb,
                "referrals": int(referrals),
                "tickets_open": int(tickets),
            },
            "costs": {
                "server_eur": SERVER_COST_EUR,
                "server_rub": round(server_rub),
                "traffic_tb": SERVER_TRAFFIC_TB,
                "gb_cost_rub": round(gb_cost_rub, 4),
                "per_user_rub": round(server_rub / max(int(total), 1), 1),
                "margin": round(total_rev_rub - server_rub),
            },
            "signups_30d": signups_formatted,
            "revenue_30d": revenue_formatted,
            "users": users_formatted,
        }
    finally:
        await pool.close()


async def get_payments(limit=500):
    """Full payments log with user details."""
    pool = await get_pool()
    try:
        rows = await pool.fetch("""
            SELECT p.id, p.telegram_id, u.username, p.method, p.amount, p.plan,
                   p.status, p.completed_at, p.created_at
            FROM payments p
            LEFT JOIN users u ON p.telegram_id = u.telegram_id
            ORDER BY p.completed_at DESC NULLS LAST, p.created_at DESC
            LIMIT $1
        """, limit)

        result = []
        for r in rows:
            completed = r["completed_at"]
            created = r["created_at"]
            ts = completed or created
            result.append({
                "id": r["id"],
                "telegram_id": r["telegram_id"],
                "username": r["username"],
                "method": r["method"],
                "amount": r["amount"],
                "plan": r["plan"],
                "status": r["status"],
                "date": ts.strftime("%Y-%m-%d") if ts else None,
                "time": ts.strftime("%H:%M") if ts else None,
                "rub": round(r["amount"] * 1.3) if r["method"] == "stars" else r["amount"],
            })
        return {"payments": result}
    finally:
        await pool.close()


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return True
        # Also accept ?token=xxx query param
        if "?" in self.path:
            params = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)
            if params.get("token") == API_TOKEN:
                return True
        return False

    def do_GET(self):
        path = self.path.split("?")[0]

        if not self.check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return

        if path == "/api/stats":
            try:
                data = asyncio.run(get_stats())
                self._json_response(200, data)
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        elif path == "/api/payments":
            try:
                data = asyncio.run(get_payments())
                self._json_response(200, data)
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        elif path == "/api/health":
            self._json_response(200, {"status": "ok"})

        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = 8080
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard API v2 running on http://localhost:{port}")
    print(f"  GET /api/stats?token={API_TOKEN}")
    print(f"  GET /api/payments?token={API_TOKEN}")
    print(f"  GET /api/health")
    server.serve_forever()
