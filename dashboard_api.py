"""StrunaVPN Dashboard API v3 — serves dashboard UI + API."""

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
API_TOKEN = os.getenv("DASHBOARD_TOKEN", "struna2026")


async def get_pool():
    return await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)


async def get_stats():
    pool = await get_pool()
    try:
        total = await pool.fetchval("SELECT COUNT(*) FROM users")
        free = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'free' OR plan_expires_at IS NULL OR plan_expires_at < NOW()")
        basic = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'basic' AND plan_expires_at > NOW()")
        pro = await pool.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'pro' AND plan_expires_at > NOW()")
        new_today = await pool.fetchval("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'")
        new_7d = await pool.fetchval("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'")
        keys = await pool.fetchval("SELECT COUNT(*) FROM users WHERE marzban_username IS NOT NULL")
        total_stars = await pool.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'stars'")
        total_rub = await pool.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'yoomoney'")
        total_payments = await pool.fetchval("SELECT COUNT(*) FROM payments WHERE status = 'completed'")
        month_stars = await pool.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'stars' AND completed_at >= DATE_TRUNC('month', NOW())")
        month_rub = await pool.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed' AND method = 'yoomoney' AND completed_at >= DATE_TRUNC('month', NOW())")
        referrals = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        tickets = await pool.fetchval("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")

        signups_rows = await pool.fetch("SELECT DATE(created_at) as d, COUNT(*) as c FROM users WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY DATE(created_at) ORDER BY d")
        revenue_rows = await pool.fetch("SELECT DATE(completed_at) as d, COUNT(*) as cnt, SUM(CASE WHEN method='stars' THEN amount ELSE 0 END) as stars, SUM(CASE WHEN method='yoomoney' THEN amount ELSE 0 END) as rub FROM payments WHERE status='completed' AND completed_at > NOW()-INTERVAL '30 days' GROUP BY DATE(completed_at) ORDER BY d")
        users_list = await pool.fetch("SELECT telegram_id, username, plan, traffic_used, traffic_limit, plan_expires_at, marzban_username, referral_count, created_at, devices_limit FROM users ORDER BY created_at DESC LIMIT 200")

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
        today = datetime.now(timezone.utc).date()
        signups_map = {r["d"]: r["c"] for r in signups_rows}
        signups_fmt = [{"date": (today - timedelta(days=i)).isoformat(), "label": (today - timedelta(days=i)).strftime("%d.%m"), "count": signups_map.get(today - timedelta(days=i), 0)} for i in range(29, -1, -1)]
        revenue_map = {r["d"]: r for r in revenue_rows}
        revenue_fmt = [{"date": (today - timedelta(days=i)).isoformat(), "label": (today - timedelta(days=i)).strftime("%d.%m"), "count": int(revenue_map[today - timedelta(days=i)]["cnt"]) if (today - timedelta(days=i)) in revenue_map else 0, "stars": int(revenue_map[today - timedelta(days=i)]["stars"]) if (today - timedelta(days=i)) in revenue_map else 0, "rub": int(revenue_map[today - timedelta(days=i)]["rub"]) if (today - timedelta(days=i)) in revenue_map else 0} for i in range(29, -1, -1)]

        now = datetime.now(timezone.utc)
        users_fmt = []
        for u in users_list:
            exp = u["plan_expires_at"]
            status = "active" if exp and exp > now else ("expired" if exp and exp <= now else "new")
            users_fmt.append({"telegram_id": u["telegram_id"], "username": u["username"], "plan": u["plan"] or "free", "traffic_gb": round(u["traffic_used"] / (1024**3), 2) if u["traffic_used"] else 0, "traffic_limit_gb": round(u["traffic_limit"] / (1024**3), 1) if u["traffic_limit"] else 0, "expires": exp.isoformat() if exp else None, "status": status, "referral_count": u["referral_count"] or 0, "devices_limit": u["devices_limit"] or 1, "marzban_username": u["marzban_username"], "created_at": u["created_at"].isoformat() if u["created_at"] else None})

        server_rub = SERVER_COST_EUR * EUR_RUB
        gb_cost_rub = server_rub / (SERVER_TRAFFIC_TB * 1024)
        paid = int(basic) + int(pro)
        total_rev_rub = int(total_stars) * 1.3 + int(total_rub)

        return {"stats": {"total_users": int(total), "free_users": int(free), "basic_users": int(basic), "pro_users": int(pro), "paid_users": paid, "keys_issued": int(keys), "online_now": online, "new_today": int(new_today), "new_7d": int(new_7d), "total_stars": int(total_stars), "total_rub": int(total_rub), "total_revenue_rub": round(total_rev_rub), "month_stars": int(month_stars), "month_rub": int(month_rub), "total_payments": int(total_payments), "traffic_gb": traffic_gb, "referrals": int(referrals), "tickets_open": int(tickets)}, "costs": {"server_eur": SERVER_COST_EUR, "server_rub": round(server_rub), "traffic_tb": SERVER_TRAFFIC_TB, "gb_cost_rub": round(gb_cost_rub, 4), "per_user_rub": round(server_rub / max(int(total), 1), 1), "margin": round(total_rev_rub - server_rub)}, "signups_30d": signups_fmt, "revenue_30d": revenue_fmt, "users": users_fmt}
    finally:
        await pool.close()


async def get_payments(limit=500):
    pool = await get_pool()
    try:
        rows = await pool.fetch("SELECT p.id, p.telegram_id, u.username, p.method, p.amount, p.plan, p.status, p.completed_at, p.created_at FROM payments p LEFT JOIN users u ON p.telegram_id = u.telegram_id ORDER BY p.completed_at DESC NULLS LAST, p.created_at DESC LIMIT $1", limit)
        result = []
        for r in rows:
            ts = r["completed_at"] or r["created_at"]
            result.append({"id": r["id"], "telegram_id": r["telegram_id"], "username": r["username"], "method": r["method"], "amount": r["amount"], "plan": r["plan"], "status": r["status"], "date": ts.strftime("%Y-%m-%d") if ts else None, "time": ts.strftime("%H:%M") if ts else None, "rub": round(r["amount"] * 1.3) if r["method"] == "stars" else r["amount"]})
        return {"payments": result}
    finally:
        await pool.close()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StrunaVPN Admin</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--surface:#111827;--surfaceAlt:#1a2234;--border:#1e293b;--accent:#22d3ee;--accentDim:rgba(34,211,238,0.15);--green:#34d399;--greenDim:rgba(52,211,153,0.15);--orange:#fb923c;--red:#f87171;--redDim:rgba(248,113,113,0.15);--purple:#a78bfa;--yellow:#fbbf24;--yellowDim:rgba(251,191,36,0.15);--text:#e2e8f0;--textDim:#64748b;--textMuted:#475569}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code',monospace;min-height:100vh}
a{color:var(--accent);text-decoration:none}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* Login */
#login-screen{display:flex;align-items:center;justify-content:center;min-height:100vh;background:radial-gradient(ellipse at 30% 20%,var(--accentDim) 0%,transparent 50%),var(--bg)}
.login-box{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:48px 40px;width:380px;text-align:center;box-shadow:0 0 60px var(--accentDim)}
.login-box h1{color:var(--accent);font-size:22px;font-weight:700;letter-spacing:2px;margin:0 0 4px}.login-box h1 span{color:var(--textDim)}
.login-box input{width:100%;padding:12px 16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;outline:none;font-family:inherit;margin-top:24px}
.login-box input.err{border-color:var(--red)}
.login-box button{width:100%;padding:12px;margin-top:16px;background:linear-gradient(135deg,var(--accent),#06b6d4);border:none;border-radius:8px;color:var(--bg);font-size:14px;font-weight:700;cursor:pointer;letter-spacing:1px;font-family:inherit}
.login-box .hint{color:var(--textMuted);font-size:11px;margin-top:20px}
.err-msg{color:var(--red);font-size:12px;margin-top:8px;display:none}

/* Dashboard */
#dashboard{display:none}
header{display:flex;align-items:center;justify-content:space-between;padding:16px 28px;border-bottom:1px solid var(--border);background:rgba(17,24,39,0.8);backdrop-filter:blur(12px);position:sticky;top:0;z-index:10}
.logo{display:flex;align-items:center;gap:12px}.logo-text{font-size:18px;font-weight:700;color:var(--accent);letter-spacing:2px}.logo-text span{color:var(--textDim)}
.badge{font-size:10px;padding:3px 8px;background:var(--accentDim);color:var(--accent);border-radius:4px;font-weight:600}
.live-dot{font-size:10px;color:var(--green)}
.tabs{display:flex;gap:4px}.tab-btn{padding:8px 14px;border-radius:8px;border:none;cursor:pointer;background:transparent;color:var(--textDim);font-size:12px;font-weight:600;font-family:inherit}
.tab-btn.active{background:var(--accentDim);color:var(--accent)}
.header-actions{display:flex;gap:8px}
.btn-icon,.btn-logout{padding:6px 12px;background:transparent;border:1px solid var(--border);border-radius:6px;color:var(--textDim);font-size:11px;cursor:pointer;font-family:inherit}
.btn-logout:hover{border-color:var(--red);color:var(--red)}
main{padding:24px 28px;max-width:1200px;margin:0 auto}
.tab-content{display:none}.tab-content.active{display:block}

/* KPI Grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:32px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;position:relative;overflow:hidden}
.kpi .bar{position:absolute;top:0;left:0;right:0;height:3px}
.kpi-label{color:var(--textDim);font-size:12px;letter-spacing:0.5px;text-transform:uppercase}
.kpi-value{color:var(--text);font-size:28px;font-weight:700;margin-top:6px}
.kpi-sub{color:var(--textMuted);font-size:12px;margin-top:4px}
.kpi-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px}

/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px}
.card-title{color:var(--textDim);font-size:13px;font-weight:500;margin-bottom:16px}
.section-hdr{display:flex;align-items:center;gap:8px;margin-bottom:16px}.section-hdr h2{color:var(--text);font-size:16px;font-weight:600}.section-hdr .line{flex:1;height:1px;background:var(--border);margin-left:8px}

/* Tables */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:10px 16px;text-align:left;color:var(--textMuted);font-size:11px;font-weight:600;letter-spacing:0.5px;text-transform:uppercase;border-bottom:1px solid var(--border)}
td{padding:10px 16px;border-bottom:1px solid var(--border)}
tr:hover td{background:var(--surfaceAlt)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%}.dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}.dot.off{background:var(--textMuted)}
.plan-badge{padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600}
.plan-basic{background:var(--accentDim);color:var(--accent)}.plan-free{background:var(--greenDim);color:var(--green)}.plan-expired{background:var(--redDim);color:var(--red)}
.method-badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.method-stars{background:var(--yellowDim);color:var(--yellow)}.method-yoomoney{background:rgba(167,139,250,0.15);color:var(--purple)}

/* Period buttons */
.period-btns{display:flex;gap:4px;margin-bottom:16px}
.period-btn{padding:7px 14px;border-radius:6px;border:none;cursor:pointer;background:var(--surface);color:var(--textDim);font-size:12px;font-weight:600;font-family:inherit}
.period-btn.active{background:var(--accentDim);color:var(--accent)}

/* Mini KPIs */
.mini-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.mini-kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 18px}
.mini-kpi-label{color:var(--textMuted);font-size:11px;text-transform:uppercase;letter-spacing:0.4px}
.mini-kpi-value{font-size:24px;font-weight:700;margin-top:4px}

/* Filters */
.filters{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}
.search-input{padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;width:260px;outline:none;font-family:inherit}
.filter-btns{display:flex;gap:4px}
.filter-btn{padding:8px 14px;border-radius:6px;border:none;cursor:pointer;background:var(--surface);color:var(--textDim);font-size:12px;font-weight:500;font-family:inherit}
.filter-btn.active{background:var(--accentDim);color:var(--accent)}
.filter-count{color:var(--textMuted);font-size:12px;margin-left:auto}

/* Charts */
.chart-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:32px}
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px}
canvas{max-height:240px}

/* Unit econ */
.econ-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px}
.econ-row{display:flex;justify-content:space-between;padding:8px 12px;border-radius:6px}
.econ-row:nth-child(odd){background:var(--surfaceAlt)}
.econ-key{color:var(--textDim)}.econ-val{font-weight:600}

/* VPN info */
.vpn-info{background:var(--surfaceAlt);border:1px solid var(--border);border-radius:12px;padding:20px;display:flex;align-items:center;gap:12px;margin-top:20px}

/* Error */
.error-banner{background:var(--redDim);border:1px solid rgba(248,113,113,0.25);border-radius:12px;padding:20px 24px;margin-bottom:24px;display:none;align-items:center;justify-content:space-between}
.error-banner .msg{color:var(--red);font-size:14px;font-weight:600}
.error-banner .detail{color:var(--textDim);font-size:12px}
.error-banner button{padding:8px 16px;background:var(--red);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}

/* Loading */
.loader{display:flex;align-items:center;justify-content:center;padding:60px;color:var(--textDim);font-size:14px}

/* Responsive */
@media(max-width:768px){
  header{flex-wrap:wrap;gap:12px;padding:12px 16px}
  main{padding:16px}
  .chart-grid{grid-template-columns:1fr}
  .kpi-grid{grid-template-columns:repeat(2,1fr)}
  .econ-grid{grid-template-columns:1fr}
  .tabs{flex-wrap:wrap}
}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login-screen">
  <div class="login-box">
    <div style="font-size:40px;margin-bottom:8px">🎸</div>
    <h1>STRUNA<span>VPN</span></h1>
    <p style="color:var(--textDim);font-size:13px;margin-bottom:8px">Admin Dashboard</p>
    <input type="password" id="pw" placeholder="Пароль...">
    <div class="err-msg" id="err-msg">Неверный пароль</div>
    <button onclick="tryLogin()">ВОЙТИ</button>
  </div>
</div>

<!-- DASHBOARD -->
<div id="dashboard">
  <header>
    <div class="logo">
      <span style="font-size:24px">🎸</span>
      <span class="logo-text">STRUNA<span>VPN</span></span>
      <span class="badge">ADMIN</span>
      <span class="live-dot" id="live-dot" style="display:none">● live</span>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('overview',this)">📊 Обзор</button>
      <button class="tab-btn" onclick="switchTab('users',this)">👥 Юзеры</button>
      <button class="tab-btn" onclick="switchTab('finance',this)">⭐ Финансы</button>
      <button class="tab-btn" onclick="switchTab('vpn',this)">🔒 VPN</button>
    </div>
    <div class="header-actions">
      <button class="btn-icon" onclick="loadData()">🔄</button>
      <button class="btn-logout" onclick="logout()">Выйти</button>
    </div>
  </header>
  <main>
    <div class="error-banner" id="error-banner"><div><div class="msg">Ошибка загрузки</div><div class="detail" id="error-detail"></div></div><button onclick="loadData()">Повторить</button></div>
    <div class="loader" id="loader">Загрузка данных с сервера...</div>
    <div id="content" style="display:none">
      <div class="tab-content active" id="tab-overview"></div>
      <div class="tab-content" id="tab-users"></div>
      <div class="tab-content" id="tab-finance"></div>
      <div class="tab-content" id="tab-vpn"></div>
    </div>
  </main>
</div>

<script>
let TOKEN = '';
let DATA = null;
let PAYMENTS = [];
let charts = {};

// ─── Auth ───
function tryLogin() {
  const pw = document.getElementById('pw').value;
  TOKEN = pw;
  fetch('/api/stats?token=' + pw).then(r => {
    if (!r.ok) throw new Error('401');
    return r.json();
  }).then(d => {
    DATA = d;
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadPayments().then(() => renderAll());
    setInterval(loadData, 60000);
  }).catch(() => {
    document.getElementById('err-msg').style.display = 'block';
    document.getElementById('pw').classList.add('err');
  });
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('pw').addEventListener('keydown', e => { if(e.key==='Enter') tryLogin() });
});

function logout() { location.reload(); }

// ─── Data loading ───
async function loadData() {
  try {
    const r = await fetch('/api/stats?token=' + TOKEN);
    DATA = await r.json();
    await loadPayments();
    document.getElementById('error-banner').style.display = 'none';
    document.getElementById('loader').style.display = 'none';
    document.getElementById('content').style.display = 'block';
    document.getElementById('live-dot').style.display = '';
    renderAll();
  } catch(e) {
    document.getElementById('error-banner').style.display = 'flex';
    document.getElementById('error-detail').textContent = e.message;
  }
}

async function loadPayments() {
  try {
    const r = await fetch('/api/payments?token=' + TOKEN);
    const d = await r.json();
    PAYMENTS = d.payments || [];
  } catch(e) { PAYMENTS = []; }
}

// ─── Tab switching ───
function switchTab(id, btn) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
}

// ─── Render ───
function renderAll() {
  if (!DATA) return;
  renderOverview();
  renderUsers();
  renderFinance();
  renderVpn();
}

function kpiHTML(label, value, sub, color, icon) {
  return `<div class="kpi"><div class="bar" style="background:linear-gradient(90deg,${color},transparent)"></div><div style="display:flex;justify-content:space-between;align-items:flex-start"><div><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>${sub?`<div class="kpi-sub">${sub}</div>`:''}</div><div class="kpi-icon" style="background:${color}18">${icon}</div></div></div>`;
}

function sectionHdr(title, icon) {
  return `<div class="section-hdr"><span style="font-size:18px">${icon}</span><h2>${title}</h2><div class="line"></div></div>`;
}

// ─── Overview ───
function renderOverview() {
  const s = DATA.stats;
  let h = `<div class="kpi-grid">`;
  h += kpiHTML('Всего юзеров', s.total_users, `+${s.new_today} сегодня / +${s.new_7d} за 7д`, 'var(--accent)', '👥');
  h += kpiHTML('Платящих', s.paid_users, `${s.basic_users} Basic / ${s.free_users} Free`, 'var(--green)', '✅');
  h += kpiHTML('Выручка', `⭐ ${s.total_stars}`, `≈ ${s.total_revenue_rub}₽ / ${s.total_payments} оплат`, 'var(--yellow)', '💰');
  h += kpiHTML('Онлайн / Тикеты', s.online_now, `${s.tickets_open} открытых тикетов`, 'var(--purple)', '🟢');
  h += `</div>`;

  h += `<div class="chart-grid"><div class="chart-box"><div class="card-title">Регистрации (7 дней)</div><canvas id="chart-signups"></canvas></div><div class="chart-box"><div class="card-title">Юзеры по планам</div><canvas id="chart-plans"></canvas></div></div>`;
  h += sectionHdr('Платежи (7 дней)', '⭐');
  h += `<div class="card"><canvas id="chart-payments7"></canvas></div>`;

  document.getElementById('tab-overview').innerHTML = h;

  // Charts
  const s7 = DATA.signups_30d.slice(-7);
  const r7 = DATA.revenue_30d.slice(-7);
  makeChart('chart-signups', 'line', s7.map(x=>x.label), [{label:'Регистрации',data:s7.map(x=>x.count),borderColor:'#22d3ee',backgroundColor:'rgba(34,211,238,0.1)',fill:true,tension:0.3}]);
  makeChart('chart-plans', 'doughnut', ['Basic','Free'], [{data:[s.basic_users,s.free_users],backgroundColor:['#34d399','#22d3ee']}], true);
  makeChart('chart-payments7', 'bar', r7.map(x=>x.label), [{label:'Stars',data:r7.map(x=>x.stars),backgroundColor:'#fbbf24',borderRadius:4}]);
}

function makeChart(id, type, labels, datasets, isDoughnut) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id);
  if (!ctx) return;
  charts[id] = new Chart(ctx, {
    type, data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: isDoughnut, labels: { color: '#64748b', font: { family: 'JetBrains Mono', size: 11 } } } },
      scales: isDoughnut ? {} : { x: { ticks: { color: '#475569', font: { size: 11 } }, grid: { color: '#1e293b' } }, y: { ticks: { color: '#475569', font: { size: 11 } }, grid: { color: '#1e293b' } } }
    }
  });
}

// ─── Users ───
let userFilter = 'all';
let userSearch = '';

function renderUsers() {
  const users = DATA.users;
  let filtered = users.filter(u => {
    const q = userSearch.toLowerCase();
    const ms = !q || (u.username||'').toLowerCase().includes(q) || String(u.telegram_id).includes(q);
    const mf = userFilter === 'all' || u.status === userFilter || u.plan === userFilter;
    return ms && mf;
  });

  const filters = [{id:'all',l:'Все'},{id:'basic',l:'Basic'},{id:'free',l:'Free'},{id:'expired',l:'Expired'},{id:'active',l:'Active'}];
  let h = `<div class="filters"><input class="search-input" placeholder="Поиск..." value="${userSearch}" oninput="userSearch=this.value;renderUsers()"><div class="filter-btns">`;
  filters.forEach(f => { h += `<button class="filter-btn ${userFilter===f.id?'active':''}" onclick="userFilter='${f.id}';renderUsers()">${f.l}</button>`; });
  h += `</div><span class="filter-count">${filtered.length} из ${users.length}</span></div>`;

  h += `<div class="card" style="padding:0;overflow:hidden"><div class="tbl-wrap"><table><thead><tr><th>Статус</th><th>Telegram ID</th><th>Username</th><th>План</th><th>Трафик</th><th>Истекает</th><th>Реф.</th><th>Дата рег.</th></tr></thead><tbody>`;
  filtered.forEach(u => {
    const exp = u.expires ? new Date(u.expires).toLocaleDateString('ru-RU') : '—';
    const cr = u.created_at ? new Date(u.created_at).toLocaleDateString('ru-RU') : '—';
    const plan = u.status === 'expired' ? 'expired' : u.plan;
    h += `<tr><td><span class="dot ${u.status==='active'?'on':'off'}"></span></td><td style="color:var(--textDim)">${u.telegram_id}</td><td style="font-weight:500">${u.username?'@'+u.username:'<span style="color:var(--textMuted)">—</span>'}</td><td><span class="plan-badge plan-${plan}">${plan}</span></td><td style="color:var(--textDim)">${u.traffic_gb} GB</td><td style="color:var(--textDim)">${exp}</td><td style="color:var(--accent)">${u.referral_count}</td><td style="color:var(--textMuted)">${cr}</td></tr>`;
  });
  h += `</tbody></table></div></div>`;
  document.getElementById('tab-users').innerHTML = h;
}

// ─── Finance ───
let finPeriod = 'all';

function renderFinance() {
  const s = DATA.stats;
  const c = DATA.costs;
  const conv = s.total_users > 0 ? ((s.paid_users / s.total_users) * 100).toFixed(1) : '0';
  const arpu = s.paid_users > 0 ? Math.round(s.total_stars / s.paid_users) : 0;
  const breakeven = Math.ceil(c.server_rub / (75 * 0.7 * 1.3));

  // Filter payments by period
  const now = new Date();
  const completed = PAYMENTS.filter(p => p.status === 'completed');
  const fp = completed.filter(p => {
    if (!p.date) return false;
    const d = new Date(p.date);
    if (finPeriod === 'today') return d.toDateString() === now.toDateString();
    if (finPeriod === '7d') return (now - d) / 864e5 <= 7;
    if (finPeriod === '30d') return (now - d) / 864e5 <= 30;
    if (finPeriod === 'month') return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
    return true;
  });
  const pStars = fp.filter(p=>p.method==='stars').reduce((a,p)=>a+p.amount,0);
  const pRub = fp.reduce((a,p)=>a+(p.rub||0),0);
  const pCount = fp.length;
  const uniq = new Set(fp.map(p=>p.telegram_id)).size;

  // Group by date
  const byDate = {};
  fp.forEach(p => {
    const d = p.date;
    if (!byDate[d]) byDate[d] = {date:d, count:0, stars:0, rub:0, users:new Set()};
    byDate[d].count++;
    if (p.method==='stars') byDate[d].stars += p.amount;
    byDate[d].rub += (p.rub||0);
    byDate[d].users.add(p.telegram_id);
  });
  const daily = Object.values(byDate).sort((a,b)=>b.date.localeCompare(a.date));

  let h = `<div class="kpi-grid">`;
  h += kpiHTML('Общая выручка', `⭐ ${s.total_stars}`, `≈ ${s.total_revenue_rub}₽`, 'var(--yellow)', '💰');
  h += kpiHTML('Конверсия', `${conv}%`, `${s.paid_users} из ${s.total_users}`, 'var(--green)', '📈');
  h += kpiHTML('ARPU', `⭐ ${arpu}`, `≈ ${Math.round(arpu*1.3)}₽`, 'var(--accent)', '👤');
  h += kpiHTML('Безубыточность', `${breakeven} юз.`, `Сервер: ${c.server_rub}₽/мес`, 'var(--orange)', '⚖️');
  h += `</div>`;

  h += sectionHdr('Детализация платежей', '💳');
  const periods = [{id:'today',l:'Сегодня'},{id:'7d',l:'7 дней'},{id:'30d',l:'30 дней'},{id:'month',l:'Месяц'},{id:'all',l:'Всё время'}];
  h += `<div class="period-btns">`;
  periods.forEach(p => { h += `<button class="period-btn ${finPeriod===p.id?'active':''}" onclick="finPeriod='${p.id}';renderFinance()">${p.l}</button>`; });
  h += `</div>`;

  h += `<div class="mini-kpi-grid">`;
  [{l:'Оплат',v:pCount,c:'var(--text)'},{l:'Stars',v:`⭐ ${pStars}`,c:'var(--yellow)'},{l:'Выручка ₽',v:`${pRub}₽`,c:'var(--green)'},{l:'Уник. плательщиков',v:uniq,c:'var(--accent)'}].forEach(x => {
    h += `<div class="mini-kpi"><div class="mini-kpi-label">${x.l}</div><div class="mini-kpi-value" style="color:${x.c}">${x.v}</div></div>`;
  });
  h += `</div>`;

  // Daily table
  h += `<div class="card" style="padding:0;overflow:hidden"><p style="color:var(--textDim);font-size:13px;font-weight:500;padding:14px 18px 0">По дням</p><div class="tbl-wrap"><table><thead><tr><th>Дата</th><th>Оплат</th><th>Юзеров</th><th>Stars</th><th>₽</th></tr></thead><tbody>`;
  if (daily.length === 0) {
    h += `<tr><td colspan="5" style="text-align:center;color:var(--textMuted);padding:20px">Нет платежей</td></tr>`;
  } else {
    daily.forEach(r => { h += `<tr><td style="font-weight:500">${r.date}</td><td>${r.count}</td><td style="color:var(--accent)">${r.users.size}</td><td style="color:var(--yellow)">⭐ ${r.stars}</td><td style="color:var(--green)">${r.rub}₽</td></tr>`; });
    h += `<tr style="background:var(--surfaceAlt);font-weight:600"><td style="color:var(--textDim)">Итого</td><td>${pCount}</td><td style="color:var(--accent)">${uniq}</td><td style="color:var(--yellow)">⭐ ${pStars}</td><td style="color:var(--green)">${pRub}₽</td></tr>`;
  }
  h += `</tbody></table></div></div>`;

  // Transactions
  h += `<div class="card" style="padding:0;overflow:hidden"><p style="color:var(--textDim);font-size:13px;font-weight:500;padding:14px 18px 0">Все транзакции (${fp.length})</p><div class="tbl-wrap"><table><thead><tr><th>#</th><th>Дата</th><th>Время</th><th>Юзер</th><th>Stars</th><th>₽</th><th>Метод</th></tr></thead><tbody>`;
  if (fp.length === 0) {
    h += `<tr><td colspan="7" style="text-align:center;color:var(--textMuted);padding:20px">Нет транзакций</td></tr>`;
  } else {
    fp.forEach(p => {
      const mCls = p.method==='stars'?'method-stars':'method-yoomoney';
      const mLabel = p.method==='stars'?'⭐ Stars':'💳 ЮMoney';
      h += `<tr><td style="color:var(--textMuted)">${p.id}</td><td style="font-weight:500">${p.date}</td><td style="color:var(--textDim)">${p.time}</td><td style="color:var(--accent)">${p.username?'@'+p.username:p.telegram_id}</td><td style="color:var(--yellow)">⭐ ${p.method==='stars'?p.amount:'—'}</td><td style="color:var(--green)">${p.rub}₽</td><td><span class="method-badge ${mCls}">${mLabel}</span></td></tr>`;
    });
  }
  h += `</tbody></table></div></div>`;

  // Chart
  h += sectionHdr('График платежей (30 дней)', '📅');
  h += `<div class="card"><canvas id="chart-revenue30"></canvas></div>`;

  // Unit economics
  h += `<div class="card"><div class="card-title">Юнит-экономика</div><div class="econ-grid">`;
  [['Цена Basic','75 Stars (≈100₽)'],['Комиссия TG','30%'],['Чистая/юзер',`≈ ${Math.round(75*0.7*1.3)}₽`],['Сервер',`${c.server_rub}₽/мес`],['1 GB',`${c.gb_cost_rub}₽`],['Трафик',`${c.traffic_tb} TB/мес`],['Маржа',`${c.margin>0?'+':''}${c.margin}₽`],['Безубыточность',`${breakeven} юзеров`]].forEach(([k,v]) => {
    const vc = v.includes('-')&&!v.includes('₽/м')?'var(--red)':v.startsWith('+')?'var(--green)':'var(--text)';
    h += `<div class="econ-row"><span class="econ-key">${k}</span><span class="econ-val" style="color:${vc}">${v}</span></div>`;
  });
  h += `</div></div>`;

  document.getElementById('tab-finance').innerHTML = h;
  const rev = DATA.revenue_30d;
  makeChart('chart-revenue30', 'bar', rev.map(x=>x.label), [{label:'Stars',data:rev.map(x=>x.stars),backgroundColor:'#fbbf24',borderRadius:4}]);
}

// ─── VPN ───
function renderVpn() {
  const s = DATA.stats;
  const top = [...DATA.users].sort((a,b)=>b.traffic_gb-a.traffic_gb).slice(0,8);

  let h = `<div class="kpi-grid">`;
  h += kpiHTML('Общий трафик', `${s.traffic_gb} GB`, `${s.keys_issued} ключей выдано`, 'var(--accent)', '📡');
  h += kpiHTML('Онлайн', s.online_now, 'подключений', 'var(--green)', '🟢');
  h += kpiHTML('Ноды', 1, 'Helsinki (Hetzner)', 'var(--purple)', '🌐');
  h += kpiHTML('Рефералы', s.referrals, 'привлечённых', 'var(--orange)', '🔗');
  h += `</div>`;

  h += sectionHdr('Серверные ноды', '🖥️');
  h += `<div class="card" style="padding:0;overflow:hidden"><div class="tbl-wrap"><table><thead><tr><th>Нода</th><th>IP</th><th>Юзеры</th><th>Трафик</th><th>Цена</th></tr></thead><tbody><tr><td style="font-weight:500">🇫🇮 Helsinki CPX22</td><td style="color:var(--textDim)">37.27.246.118</td><td>${s.total_users}</td><td style="color:var(--textDim)">${s.traffic_gb} GB</td><td style="color:var(--textDim)">€7.72/мес</td></tr></tbody></table></div></div>`;

  h += sectionHdr('Топ по трафику', '📊');
  h += `<div class="card"><canvas id="chart-traffic-top"></canvas></div>`;

  h += `<div class="vpn-info"><span style="font-size:20px">⚙️</span><div><p style="font-size:13px;font-weight:600;margin-bottom:4px">VLESS + Reality</p><p style="color:var(--textMuted);font-size:12px">Xray 24.12.31 • Порт 443 • dest: google.com • flow: xtls-rprx-vision • Hiddify</p></div></div>`;

  document.getElementById('tab-vpn').innerHTML = h;

  if (top.length > 0) {
    makeChart('chart-traffic-top', 'bar', top.map(u => u.username ? '@'+u.username : 'anon'), [{label:'GB',data:top.map(u=>u.traffic_gb),backgroundColor:'#22d3ee',borderRadius:4}]);
  }
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _get_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        if "?" in self.path:
            params = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)
            return params.get("token", "")
        return ""

    def do_GET(self):
        path = self.path.split("?")[0]

        # Dashboard HTML — no auth needed (login is in-page)
        if path == "/" or path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
            return

        # API endpoints — require token
        if path.startswith("/api/"):
            if path == "/api/health":
                self._json(200, {"status": "ok"})
                return

            if self._get_token() != API_TOKEN:
                self._json(401, {"error": "unauthorized"})
                return

            if path == "/api/stats":
                try:
                    self._json(200, asyncio.run(get_stats()))
                except Exception as e:
                    self._json(500, {"error": str(e)})
            elif path == "/api/payments":
                try:
                    self._json(200, asyncio.run(get_payments()))
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self.send_response(404)
                self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"StrunaVPN Dashboard v3")
    print(f"  UI:       http://0.0.0.0:{port}/")
    print(f"  API:      http://0.0.0.0:{port}/api/stats?token={API_TOKEN}")
    print(f"  Payments: http://0.0.0.0:{port}/api/payments?token={API_TOKEN}")
    server.serve_forever()
