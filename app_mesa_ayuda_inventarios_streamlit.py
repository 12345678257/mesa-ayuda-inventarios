# app_mesa_ayuda_inventarios_streamlit.py
# ENTERPRISE+ : Mesa de Ayuda + Inventarios (ITIL 4) con extras enterprise
# - Aprobaciones multinivel por áreas (configurables)
# - Evaluación de riesgo automática para Cambios
# - Matriz urgencia×impacto configurable por servicio (catálogo)
# - Encuestas CSAT/CES/NPS
# - Vistas por equipo (teams)
# - Webhooks (salientes) para integrar con otras plataformas
# - SSO sencillo por token en query string (HMAC)
import os, re, secrets, hashlib, sqlite3, json, hmac
from datetime import datetime, timedelta
from typing import List, Optional
import pandas as pd
import streamlit as st
import altair as alt

APP_TITLE = "Help Desk & Inventarios – ITIL Enterprise+"
DB_PATH = os.environ.get("APP_DB_PATH") or os.path.join(os.path.dirname(__file__), "inventarios_helpdesk.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
SSO_SECRET = os.environ.get("APP_SSO_SECRET", "")  # opcional para SSO por token

def ensure_dirs(): os.makedirs(UPLOAD_DIR, exist_ok=True)
def conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

# --- DB helpers con auto-migración ---
def _retry_migrating(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except Exception as e:
        if any(k in str(e) for k in ["no such column","has no column","no such table"]):
            migrate_schema(); return fn(*args, **kwargs)
        raise

def run_script(sql, params=()):
    def _exec():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            cx.execute(sql, params); cx.commit()
    return _retry_migrating(_exec)

def run_query(sql, params=()):
    def _q():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            return pd.read_sql_query(sql, cx, params=params)
    return _retry_migrating(_q)

def hash_password(p, salt): return hashlib.sha256((salt + p).encode("utf-8")).hexdigest()

# --- Esquema base (ENTERPRISE+) ---
INIT_SQL = r"""
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL, email TEXT,
  password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','agente','visor')),
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS user_teams (user_id INTEGER NOT NULL, team_id INTEGER NOT NULL, UNIQUE(user_id, team_id));
CREATE TABLE IF NOT EXISTS warehouses (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, location TEXT);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, contact TEXT, email TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, brand TEXT, model TEXT, barcode TEXT, uom TEXT,
  usage_type TEXT NOT NULL DEFAULT 'Administrativo' CHECK(usage_type IN ('Administrativo','Asistencial')),
  category_id INTEGER, supplier_id INTEGER, unit_cost REAL NOT NULL DEFAULT 0, min_stock REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stock (product_id INTEGER, warehouse_id INTEGER, qty REAL NOT NULL DEFAULT 0, PRIMARY KEY(product_id, warehouse_id));
CREATE TABLE IF NOT EXISTS movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL CHECK(type IN ('ENTRADA','SALIDA','TRANSFERENCIA','AJUSTE')),
  product_id INTEGER NOT NULL, from_wh INTEGER, to_wh INTEGER, qty REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0, reason TEXT,
  created_by INTEGER, created_at TEXT NOT NULL
);
-- Activos (para relacionar tickets/activos)
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, serial TEXT, status TEXT DEFAULT 'Operativo', active INTEGER DEFAULT 1
);
-- CMDB
CREATE TABLE IF NOT EXISTS ci_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, ci_type TEXT NOT NULL, criticality TEXT CHECK(criticality IN ('Baja','Media','Alta','Crítica')),
  owner TEXT, location TEXT, status TEXT DEFAULT 'Operativo', attributes TEXT
);
CREATE TABLE IF NOT EXISTS ci_relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_ci_id INTEGER NOT NULL, child_ci_id INTEGER NOT NULL, relation_type TEXT NOT NULL
);
-- SLA / Catálogo / Matriz
CREATE TABLE IF NOT EXISTS sla_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  process TEXT NOT NULL CHECK(process IN ('Incidente','Solicitud','Cambio','Problema')),
  priority TEXT NOT NULL CHECK(priority IN ('Baja','Media','Alta','Crítica')),
  response_hours INTEGER NOT NULL, resolution_hours INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS priority_matrix (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  urgency TEXT NOT NULL CHECK(urgency IN ('Baja','Media','Alta','Crítica')),
  impact TEXT NOT NULL CHECK(impact IN ('Bajo','Medio','Alto','Crítico')),
  priority TEXT NOT NULL CHECK(priority IN ('Baja','Media','Alta','Crítica')),
  UNIQUE(urgency, impact)
);
CREATE TABLE IF NOT EXISTS service_catalog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER, name TEXT UNIQUE NOT NULL,
  process TEXT NOT NULL CHECK(process IN ('Incidente','Solicitud','Cambio','Problema')),
  default_priority TEXT NOT NULL CHECK(default_priority IN ('Baja','Media','Alta','Crítica')),
  policy_id INTEGER, owner_email TEXT
);
-- Matriz por servicio (sobre-escribe a la global si existe combinación)
CREATE TABLE IF NOT EXISTS service_priority_matrix (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  catalog_id INTEGER NOT NULL, urgency TEXT NOT NULL, impact TEXT NOT NULL, priority TEXT NOT NULL,
  UNIQUE(catalog_id, urgency, impact)
);
CREATE TABLE IF NOT EXISTS kb_articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT,
  created_by INTEGER, created_at TEXT NOT NULL
);
-- Tickets + aprobaciones + encuestas
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT,
  category TEXT NOT NULL CHECK(category IN ('Incidente','Solicitud','Ajuste','Consulta')),
  priority TEXT NOT NULL CHECK(priority IN ('Baja','Media','Alta','Crítica')),
  urgency TEXT CHECK(urgency IN ('Baja','Media','Alta','Crítica')),
  impact TEXT CHECK(impact IN ('Bajo','Medio','Alto','Crítico')),
  status TEXT NOT NULL CHECK(status IN ('Abierto','En Progreso','Resuelto','Cerrado')),
  sla_hours INTEGER NOT NULL DEFAULT 48, response_sla_hours INTEGER NOT NULL DEFAULT 4,
  first_response_at TEXT, resolved_at TEXT,
  itil_type TEXT, change_risk TEXT, change_impact TEXT, planned_start TEXT, planned_end TEXT,
  approval_mgr_status TEXT, approval_cab_status TEXT, approval_status TEXT,
  backout_plan TEXT, problem_root_cause TEXT, problem_workaround TEXT, problem_id INTEGER,
  created_by INTEGER, assigned_to INTEGER, warehouse_id INTEGER, product_id INTEGER, asset_id INTEGER, ci_id INTEGER,
  catalog_id INTEGER, watchers_emails TEXT, attachment_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, due_at TEXT, closed_at TEXT
);
-- Aprobaciones básicas + multinivel (áreas)
CREATE TABLE IF NOT EXISTS change_approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL, level TEXT NOT NULL,
  approver TEXT, approver_email TEXT, status TEXT NOT NULL DEFAULT 'Pendiente' CHECK(status IN ('Pendiente','Aprobado','Rechazado')),
  decided_at TEXT, comment TEXT
);
CREATE TABLE IF NOT EXISTS approval_areas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, emails TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS service_approval_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER NOT NULL, area_name TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ticket_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  author_id INTEGER,
  comment TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- Encuestas
CREATE TABLE IF NOT EXISTS ticket_surveys (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, user_id INTEGER, rating INTEGER CHECK(rating BETWEEN 1 AND 5), comment TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS surveys_ces (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, user_id INTEGER, score INTEGER CHECK(score BETWEEN 1 AND 7), comment TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS surveys_nps (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, score INTEGER CHECK(score BETWEEN 0 AND 10), comment TEXT, created_at TEXT NOT NULL);
-- Riesgo (reglas opcionales)
CREATE TABLE IF NOT EXISTS risk_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ci_criticality TEXT, change_impact TEXT, priority TEXT, risk_result TEXT
);
-- Config
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, table_name TEXT NOT NULL, record_id TEXT, user TEXT, created_at TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS password_resets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
"""

SMTP_DEFAULTS = {
  "smtp_server":"smtp.office365.com","smtp_port":"587","smtp_use_tls":"1","smtp_username":"","smtp_password":"","smtp_from":"",
  "notif_on_create":"1","notif_on_resolve":"1","notif_on_status_change":"1","notif_on_comment":"1","notif_on_approval":"1",
  "notif_default_to":"","cab_emails":"","mgr_emails":"",
  "webhook_url":"","webhook_token":""
}

def _column_exists(table: str, column: str) -> bool:
    df = run_query(f"PRAGMA table_info({table})"); return (not df.empty) and (column in df['name'].tolist())

def migrate_schema():
    with conn() as cx: cx.executescript(INIT_SQL)
    # Añadir columnas faltantes en tickets para compatibilidad
    add_cols_tickets = [
        ('asset_id','INTEGER',None),('ci_id','INTEGER',None),('catalog_id','INTEGER',None),('watchers_emails','TEXT',"''"),
        ('attachment_path','TEXT','NULL'),('sla_hours','INTEGER','48'),('response_sla_hours','INTEGER','4'),
        ('first_response_at','TEXT','NULL'),('resolved_at','TEXT','NULL'),('itil_type','TEXT',"'Incidente'"),
        ('change_risk','TEXT','NULL'),('change_impact','TEXT','NULL'),
        ('planned_start','TEXT','NULL'),('planned_end','TEXT','NULL'),
        ('approval_mgr_status','TEXT',"'Pendiente'"),('approval_cab_status','TEXT',"'Pendiente'"),('approval_status','TEXT',"'Pendiente'"),
        ('backout_plan','TEXT','NULL'),('problem_root_cause','TEXT','NULL'),('problem_workaround','TEXT','NULL'),
        ('problem_id','INTEGER','NULL'),('urgency','TEXT','NULL'),('impact','TEXT','NULL')
    ]
    for col, typ, default_sql in add_cols_tickets:
        if not _column_exists('tickets', col):
            run_script(f"ALTER TABLE tickets ADD COLUMN {col} {typ}")
            if default_sql is not None: run_script(f"UPDATE tickets SET {col} = {default_sql} WHERE {col} IS NULL")
    # productos
    for col, typ, default_sql in [('brand','TEXT','NULL'),('model','TEXT','NULL'),('barcode','TEXT','NULL'),('uom','TEXT','NULL'),('usage_type','TEXT',"'Administrativo'")]:
        if not _column_exists('products', col):
            run_script(f"ALTER TABLE products ADD COLUMN {col} {typ}")
            if default_sql is not None: run_script(f"UPDATE products SET {col} = {default_sql} WHERE {col} IS NULL")
    # assets
    try:
        run_script("CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, serial TEXT, status TEXT DEFAULT 'Operativo', active INTEGER DEFAULT 1)")
    except Exception: pass
    # defaults settings
    for k, v in SMTP_DEFAULTS.items():
        if run_query("SELECT 1 FROM settings WHERE key=?", (k,)).empty:
            run_script("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))

def init_db():
    ensure_dirs()
    with conn() as cx: cx.executescript(INIT_SQL)
    migrate_schema()
    if int(run_query("SELECT COUNT(*) n FROM users").loc[0,'n']) == 0:
        salt = secrets.token_hex(8)
        run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                   ("admin","admin@example.com",hash_password("admin", salt),salt,"admin",datetime.utcnow().isoformat()))
        run_script("INSERT OR IGNORE INTO warehouses(name,location) VALUES(?,?)", ("Bodega Central","Bogotá"))
        run_script("INSERT OR IGNORE INTO categories(name) VALUES(?)", ("General",))
        run_script("INSERT OR IGNORE INTO suppliers(name,contact,email,phone) VALUES(?,?,?,?)", ("Proveedor Demo","Contacto","proveedor@demo.com","3000000000"))
        run_script("INSERT OR IGNORE INTO products(sku,name,brand,model,barcode,uom,usage_type,category_id,supplier_id,unit_cost,min_stock,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                   ("SKU-001","Producto Demo","DemoBrand","X1","000111222333","UND","Administrativo",1,1,1000,10))
        run_script("INSERT OR IGNORE INTO assets(code,name,serial,status,active) VALUES(?,?,?,?,1)", ("AS-0001","Laptop Demo","SRL-001","Operativo"))
        # SLA por defecto
        defaults = [
            ("Incidente","Baja",8,72),("Incidente","Media",4,48),("Incidente","Alta",2,24),("Incidente","Crítica",1,8),
            ("Solicitud","Baja",12,96),("Solicitud","Media",6,72),("Solicitud","Alta",2,36),("Solicitud","Crítica",1,16),
            ("Cambio","Media",8,72),("Problema","Media",8,120)
        ]
        for pr in defaults:
            run_script("INSERT INTO sla_policies(process,priority,response_hours,resolution_hours,active) VALUES(?,?,?,?,1)", pr)
        # Matriz global
        matrix = [
            ("Baja","Bajo","Baja"),("Baja","Medio","Baja"),("Baja","Alto","Media"),("Baja","Crítico","Alta"),
            ("Media","Bajo","Baja"),("Media","Medio","Media"),("Media","Alto","Alta"),("Media","Crítico","Alta"),
            ("Alta","Bajo","Media"),("Alta","Medio","Alta"),("Alta","Alto","Alta"),("Alta","Crítico","Crítica"),
            ("Crítica","Bajo","Alta"),("Crítica","Medio","Alta"),("Crítica","Alto","Crítica"),("Crítica","Crítico","Crítica")
        ]
        for u,i,p in matrix:
            run_script("INSERT OR IGNORE INTO priority_matrix(urgency,impact,priority) VALUES(?,?,?)", (u,i,p))
        # Áreas de aprobación demo
        run_script("INSERT OR IGNORE INTO approval_areas(name,emails,active) VALUES(?,?,1)", ("Seguridad","seguridad@example.com",))
        run_script("INSERT OR IGNORE INTO approval_areas(name,emails,active) VALUES(?,?,1)", ("Operaciones","ops@example.com",))
        # Equipo demo
        run_script("INSERT OR IGNORE INTO teams(name) VALUES(?)", ("Soporte N1",))
    # settings por defecto
    for k, v in SMTP_DEFAULTS.items():
        if run_query("SELECT 1 FROM settings WHERE key=?", (k,)).empty:
            run_script("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))

def get_setting(key, default=None):
    df = run_query("SELECT value FROM settings WHERE key=?", (key,)); 
    return default if df.empty else df.loc[0,'value']

def set_setting(key, value):
    if run_query("SELECT 1 FROM settings WHERE key=?", (key,)).empty:
        run_script("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    else:
        run_script("UPDATE settings SET value=? WHERE key=?", (value, key))

# --- SSO por token en query string ---
def try_sso_login():
    if not SSO_SECRET: return
    qp = st.query_params
    token = qp.get("sig", None); user = qp.get("user", None); ts = qp.get("ts", None)
    if not token or not user or not ts: return
    try:
        ts = int(ts)
        if abs(int(datetime.utcnow().timestamp()) - ts) > 600:  # 10 min
            return
        msg = f"{user}:{ts}".encode("utf-8")
        expected = hmac.new(SSO_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, token): return
        # login o autoprovisión
        df = run_query("SELECT * FROM users WHERE username=?", (user,))
        if df.empty:
            salt = secrets.token_hex(8)
            run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                       (user, f"{user}@example.com", hash_password(secrets.token_urlsafe(12), salt), salt, "visor", datetime.utcnow().isoformat()))
            df = run_query("SELECT * FROM users WHERE username=?", (user,))
        row = df.iloc[0]
        st.session_state["auth_user"] = {"id": int(row["id"]), "username": row["username"], "role": row["role"], "email": row.get("email","")}
        st.toast("Autenticación SSO correcta")
    except Exception:
        return

# --- Email / Webhooks ---
def _smtp_enabled(): return bool(get_setting("smtp_server")) and bool(get_setting("smtp_from"))
def send_email(to_addrs: List[str], subject: str, html_body: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    server = get_setting("smtp_server",""); port = int(get_setting("smtp_port","587") or 587)
    use_tls = get_setting("smtp_use_tls","1")=="1"
    username = get_setting("smtp_username","") or None
    password = os.getenv("APP_SMTP_PASSWORD") or (get_setting("smtp_password","") or None)
    from_addr = get_setting("smtp_from","") or (username or "")
    if not server or not from_addr: return False
    msg = MIMEMultipart(); msg["From"]=from_addr; msg["To"]=", ".join([a for a in to_addrs if a]); msg["Subject"]=subject
    msg.attach(MIMEText(html_body, "html"))
    try:
        smtp = smtplib.SMTP(server, port, timeout=30)
        if use_tls: smtp.starttls()
        if username and password: smtp.login(username, password)
        smtp.sendmail(from_addr, to_addrs, msg.as_string()); smtp.quit(); return True
    except Exception:
        return False

def post_webhook(event: str, payload: dict):
    url = get_setting("webhook_url",""); token = get_setting("webhook_token","")
    if not url: return
    try:
        import requests
        headers = {"Content-Type":"application/json"}
        if token: headers["Authorization"] = f"Bearer {token}"
        requests.post(url, headers=headers, data=json.dumps({"event":event,"payload":payload}), timeout=5)
    except Exception:
        pass

def _collect_recipients(creator_email, assignee_email, watchers, extra=None):
    rec = []; 
    if creator_email: rec.append(creator_email)
    if assignee_email: rec.append(assignee_email)
    add = [e.strip() for e in (get_setting("notif_default_to","") or "").split(",") if e.strip()]
    rec += add + [w for w in (watchers or []) if w] + ([e.strip() for e in (extra or []) if e.strip()])
    seen=set(); out=[]; 
    for r in rec:
        if r and r not in seen: out.append(r); seen.add(r)
    return out

def _ticket_html_summary(t):
    assignee = t.get('assignee_name') or t.get('asignado') or "—"
    rows = [("Código",t['code']),("Título",t['title']),("Proceso",t.get('itil_type','Incidente')),("Urgencia",t.get('urgency',"N/A")),("Impacto",t.get('impact',"N/A")),("Prioridad",t['priority']),("Estado",t['status']),("Vence",t.get('due_at') or "N/A"),("Asignado a", assignee)]
    tr = "".join([f"<tr><td style='padding:4px 8px'><b>{k}</b></td><td style='padding:4px 8px'>{v}</td></tr>" for k,v in rows])
    return f"<table border='0' cellspacing='0' cellpadding='0'>{tr}</table>"

def _notify(subject, html, creator_email, assignee_email, watchers, extra=None):
    if not _smtp_enabled(): return
    to_list = _collect_recipients(creator_email, assignee_email, watchers, extra=extra)
    if to_list: send_email(to_list, subject, html)

def notify_ticket_created(t, creator_email, assignee_email, watchers, extra=None):
    if get_setting("notif_on_create","1")!="1": return
    _notify(f"[Nuevo] Ticket {t['code']}: {t['title']}", f"<h3>Nuevo Ticket</h3>{_ticket_html_summary(t)}", creator_email, assignee_email, watchers, extra); post_webhook("ticket_created", t)

def notify_ticket_assigned(t, creator_email, assignee_email, watchers):
    _notify(f"[Asignación] Ticket {t['code']}: {t['title']}", f"<h3>Ticket asignado</h3>{_ticket_html_summary(t)}", creator_email, assignee_email, watchers); post_webhook("ticket_assigned", t)

def notify_ticket_status_change(t, creator_email, assignee_email, watchers, old_status):
    if get_setting("notif_on_status_change","1")!="1": return
    payload = dict(t); payload["old_status"] = old_status
    _notify(f"[Estado: {t['status']}] Ticket {t['code']}: {t['title']}", f"<h3>Estado actualizado</h3><p><b>{old_status}</b> → <b>{t['status']}</b></p>{_ticket_html_summary(t)}", creator_email, assignee_email, watchers); post_webhook("ticket_status_changed", payload)

def notify_ticket_commented(t, creator_email, assignee_email, watchers, comment_preview):
    if get_setting("notif_on_comment","1")!="1": return
    payload = dict(t); payload["comment_preview"]=comment_preview
    _notify(f"[Comentario] Ticket {t['code']}: {t['title']}", f"<h3>Nuevo comentario</h3><p>{comment_preview}</p>{_ticket_html_summary(t)}", creator_email, assignee_email, watchers); post_webhook("ticket_commented", payload)

def notify_ticket_resolved(t, creator_email, watchers):
    if get_setting("notif_on_resolve","1")!="1": return
    _notify(f"[Resuelto] Ticket {t['code']}: {t['title']}", f"<h3>Ticket Resuelto</h3>{_ticket_html_summary(t)}", creator_email, None, watchers); post_webhook("ticket_resolved", t)

def notify_approval_requested(t, level):
    if get_setting("notif_on_approval","1")!="1": return
    _notify(f"[Aprobación {level}] Ticket {t['code']}", f"<h3>Se requiere aprobación de {level}</h3>{_ticket_html_summary(t)}", t.get('creator_email'), t.get('assignee_email'), [e.strip() for e in (t.get('watchers','') or '').split(',') if e.strip()], None); post_webhook("approval_requested", {"level":level, **t})

def notify_approval_decided(t, level, status, approver):
    if get_setting("notif_on_approval","1")!="1": return
    payload = dict(t); payload.update({"level":level,"decision":status,"approver":approver})
    subj = f"[Aprobación {level}: {status}] Ticket {t['code']}"
    html = f"<h3>Aprobación {level}: {status}</h3><p>Por: <b>{approver}</b></p>{_ticket_html_summary(t)}"
    _notify(subj, html, t.get('creator_email'), t.get('assignee_email'), [e.strip() for e in (t.get('watchers','') or '').split(',') if e.strip()], None); post_webhook("approval_decided", payload)

# --- Auth / registro / recuperación ---
SESSION_USER_KEY = "auth_user"

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "", s.replace(" ", ""))
    return s[:20] or "user"

def _username_available(u: str) -> bool: return run_query("SELECT 1 FROM users WHERE username=?", (u,)).empty

def start_password_reset(identifier: str):
    df = run_query("SELECT * FROM users WHERE (username=? OR email=?) AND active=1", (identifier.strip(), identifier.strip()))
    if df.empty: return None
    row = df.iloc[0]; token = secrets.token_urlsafe(32)
    now = datetime.utcnow(); exp = now + timedelta(hours=1)
    run_script("INSERT INTO password_resets(user_id, token, created_at, expires_at, used) VALUES(?,?,?,?,0)", (int(row['id']), token, now.isoformat(), exp.isoformat()))
    if _smtp_enabled() and (row.get('email') or ""): send_email([row['email']], "Recuperar contraseña – Mesa de Ayuda", f"<p>Usuario: <b>{row['username']}</b><br/>Código: <b>{token}</b> (1h de validez)</p>")
    return token

def complete_password_reset(token: str, new_password: str) -> bool:
    df = run_query("SELECT * FROM password_resets WHERE token=? AND used=0", (token,))
    if df.empty: return False
    row = df.iloc[0]
    if datetime.utcnow() > datetime.fromisoformat(row['expires_at']): return False
    uid = int(row['user_id']); salt = secrets.token_hex(8)
    run_script("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(new_password, salt), salt, uid))
    run_script("UPDATE password_resets SET used=1 WHERE id=?", (int(row['id']),)); return True

def require_login(): return st.session_state.get(SESSION_USER_KEY)

def login_ui():
    st.title(APP_TITLE); st.caption("Usuario inicial **admin / admin**. También disponible SSO por token si se configura `APP_SSO_SECRET`.")
    mode = st.session_state.get("mode","login")
    if mode=="signup": return signup_ui()
    if mode=="forgot": return forgot_ui()
    if mode=="reset": return reset_ui()
    with st.form("login_form"):
        user = st.text_input("Usuario"); pwd = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar")
    if submitted:
        df = run_query("SELECT * FROM users WHERE username=? AND active=1", (user.strip(),))
        if df.empty: st.error("Usuario no encontrado o inactivo.")
        else:
            row = df.iloc[0]
            if hash_password(pwd, row["password_salt"]) == row["password_hash"]:
                st.session_state[SESSION_USER_KEY] = {"id": int(row["id"]), "username": row["username"], "role": row["role"], "email": row.get("email","")}; st.success(f"¡Bienvenido, {row['username']}!"); st.rerun()
            else: st.error("Contraseña incorrecta.")
    c1, c2, c3 = st.columns(3)
    if c1.button("🆕 Registrarme"): st.session_state["mode"]="signup"; st.rerun()
    if c2.button("¿Olvidaste tu contraseña?"): st.session_state["mode"]="forgot"; st.rerun()
    if c3.button("Tengo un código de recuperación"): st.session_state["mode"]="reset"; st.rerun()

def signup_ui():
    st.header("🆕 Crear cuenta (rol visor)")
    with st.form("signup_form"):
        full_name = st.text_input("Nombre completo"); email = st.text_input("Email *"); submitted = st.form_submit_button("Crear mi cuenta")
    if submitted:
        if not email or "@" not in email: st.error("Email inválido."); return
        base = (email.split('@')[0] or _slug(full_name)); username = _slug(base)
        if not _username_available(username):
            n=1
            while not _username_available(f"{username}{n}") and n<9999: n+=1
            username = f"{username}{n}"
        pwd_plain = secrets.token_urlsafe(12); salt = secrets.token_hex(8)
        run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)", (username, email.strip(), hash_password(pwd_plain, salt), salt, "visor", datetime.utcnow().isoformat()))
        st.success("✅ Cuenta creada."); st.info(f"**Tu usuario:** `{username}`  \n**Tu contraseña:** `{pwd_plain}`")
        if _smtp_enabled(): send_email([email.strip()], "Credenciales de acceso – Mesa de Ayuda", f"<p>Usuario: <b>{username}</b><br/>Contraseña: <b>{pwd_plain}</b></p>")
    if st.button("⬅️ Volver a Iniciar sesión"): st.session_state["mode"]="login"; st.rerun()

def forgot_ui():
    st.header("🔑 Recuperar contraseña")
    with st.form("forgot_form"):
        ident = st.text_input("Tu usuario o email *"); submitted = st.form_submit_button("Enviar código")
    if submitted:
        token = start_password_reset(ident)
        if token is None: st.error("Usuario/email no encontrado o inactivo.")
        else: st.success("Código generado (se envió por email si SMTP está configurado)."); st.code(token)
    if st.button("Ya tengo un código"): st.session_state["mode"]="reset"; st.rerun()
    if st.button("⬅️ Volver a inicio"): st.session_state["mode"]="login"; st.rerun()

def reset_ui():
    st.header("🔒 Ingresar código de recuperación")
    with st.form("reset_form"):
        token = st.text_input("Código recibido *"); pwd1 = st.text_input("Nueva contraseña *", type="password"); pwd2 = st.text_input("Confirmar nueva contraseña *", type="password")
        submitted = st.form_submit_button("Cambiar contraseña")
    if submitted:
        if not token or not pwd1 or not pwd2: st.error("Completa todos los campos.")
        elif pwd1 != pwd2: st.error("Las contraseñas no coinciden.")
        else:
            ok = complete_password_reset(token.strip(), pwd1)
            if ok: st.success("Contraseña actualizada. Inicia sesión."); st.session_state["mode"]="login"; st.rerun()
            else: st.error("Código inválido o expirado.")
    if st.button("⬅️ Volver a inicio"): st.session_state["mode"]="login"; st.rerun()

# --- SLA / Catálogo / Matrices ---
def sla_lookup(process: str, priority: str):
    df = run_query("SELECT response_hours, resolution_hours FROM sla_policies WHERE process=? AND priority=? AND active=1 LIMIT 1", (process, priority))
    if df.empty: return (4, 48)
    return int(df.loc[0,'response_hours']), int(df.loc[0,'resolution_hours'])

def matrix_priority_global(urgency: str, impact: str) -> str:
    df = run_query("SELECT priority FROM priority_matrix WHERE urgency=? AND impact=? LIMIT 1", (urgency, impact))
    return "Media" if df.empty else df.loc[0,'priority']

def matrix_priority_service(catalog_id: Optional[int], urgency: str, impact: str) -> str:
    if not catalog_id: return matrix_priority_global(urgency, impact)
    df = run_query("SELECT priority FROM service_priority_matrix WHERE catalog_id=? AND urgency=? AND impact=? LIMIT 1", (catalog_id, urgency, impact))
    return df.loc[0,'priority'] if not df.empty else matrix_priority_global(urgency, impact)

def kb_search(q: str, limit=5):
    if not q: return pd.DataFrame()
    return run_query("SELECT code,title,tags FROM kb_articles WHERE title LIKE ? OR tags LIKE ? ORDER BY id DESC LIMIT ?", (f"%{q}%", f"%{q}%", limit))

def state_transition_ok(old: str, new: str) -> bool:
    allowed = {"Abierto":["En Progreso","Cerrado"],"En Progreso":["Resuelto","Cerrado"],"Resuelto":["Cerrado","En Progreso"],"Cerrado":[]}
    return new in allowed.get(old, [])

# Evaluación automática de riesgo de cambio
def auto_change_risk(ci_id: Optional[int], change_impact: Optional[str], priority: str) -> str:
    # 1) Reglas en tabla risk_rules si existen
    crit = None
    if ci_id:
        dfc = run_query("SELECT criticality FROM ci_items WHERE id=?", (ci_id,))
        if not dfc.empty: crit = dfc.loc[0,'criticality']
    rr = run_query("SELECT risk_result FROM risk_rules WHERE (ci_criticality IS NULL OR ci_criticality=?) AND (change_impact IS NULL OR change_impact=?) AND (priority IS NULL OR priority=?) LIMIT 1", (crit, change_impact, priority))
    if not rr.empty: return rr.loc[0,'risk_result']
    # 2) Heurística por defecto
    score = 0
    score += {"Baja":0,"Media":1,"Alta":2,"Crítica":3}.get(crit or "Media", 1)
    score += {"Bajo":0,"Medio":1,"Alto":2,"Crítico":3}.get((change_impact or "Medio"), 1)
    score += {"Baja":0,"Media":1,"Alta":2,"Crítica":3}.get(priority, 1)
    if score >= 7: return "Alto"
    if score >= 5: return "Medio"
    return "Bajo"

# --- Inventario ---
def df_products():
    return run_query("""SELECT p.id, p.sku, p.name AS producto, p.brand AS marca, p.model AS modelo, p.barcode AS codigo_barras,
                               p.uom AS unidad, p.usage_type AS tipo, c.name AS categoria, s.name AS proveedor,
                               p.unit_cost AS costo_unit, p.min_stock AS stock_min, p.active
                        FROM products p
                        LEFT JOIN categories c ON c.id = p.category_id
                        LEFT JOIN suppliers s ON s.id = p.supplier_id
                        ORDER BY p.name""")

def df_stock():
    return run_query("""SELECT s.product_id, s.warehouse_id, w.name AS bodega, p.name AS producto, p.usage_type AS tipo, s.qty, p.unit_cost, (s.qty*p.unit_cost) AS valor
                        FROM stock s JOIN products p ON p.id=s.product_id JOIN warehouses w ON w.id=s.warehouse_id
                        ORDER BY p.name, w.name""")

def adjust_stock(product_id, warehouse_id, delta):
    with conn() as cx:
        cur = cx.execute("SELECT qty FROM stock WHERE product_id=? AND warehouse_id=?", (product_id, warehouse_id))
        row = cur.fetchone()
        if row is None: cx.execute("INSERT INTO stock(product_id,warehouse_id,qty) VALUES(?,?,?)",(product_id,warehouse_id,max(0,delta)))
        else:
            new_qty = max(0, float(row[0]) + float(delta)); cx.execute("UPDATE stock SET qty=? WHERE product_id=? AND warehouse_id=?", (new_qty, product_id, warehouse_id))
        cx.commit()

# --- UI: Dashboard ---
def page_dashboard():
    st.title("📊 Dashboard")
    user = require_login(); role = user['role']
    stock_df = df_stock(); total_valor = float(stock_df["valor"].sum()) if not stock_df.empty else 0.0
    low_df = run_query("""SELECT p.id, p.name AS producto, COALESCE(SUM(s.qty),0) AS stock_total, p.min_stock
                          FROM products p LEFT JOIN stock s ON s.product_id=p.id GROUP BY p.id
                          HAVING stock_total < p.min_stock ORDER BY stock_total ASC""")
    tickets_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE status IN ('Abierto','En Progreso')")
    tickets_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (datetime.utcnow().isoformat(),))
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Valor inventario", f"${total_valor:,.0f}")
    c2.metric("Productos bajo stock", int(low_df.shape[0]))
    c3.metric("Tickets abiertos", int(tickets_open.loc[0,'n']))
    c4.metric("Tickets vencidos", int(tickets_overdue.loc[0,'n']))
    ch = run_query("SELECT code,title,planned_start,planned_end,approval_status FROM tickets WHERE itil_type='Cambio' AND planned_start IS NOT NULL ORDER BY planned_start DESC LIMIT 20")
    if not ch.empty:
        st.subheader("📅 Cambios planificados (últimos)"); st.dataframe(ch, use_container_width=True)
    tdf = run_query("SELECT status, priority, COUNT(*) as n FROM tickets GROUP BY status, priority ORDER BY status, priority")
    if not tdf.empty:
        chart = alt.Chart(tdf).mark_bar().encode(x=alt.X('status:N', title='Estado'), y=alt.Y('n:Q', title='Cantidad'), color='priority:N', tooltip=['status','priority','n']).properties(height=320)
        st.altair_chart(chart, use_container_width=True)

# --- UI: Inventario Productos / Movimientos / CMDB (similares a Enterprise, omitidos por brevedad) ---
# Para mantener el script compacto, reusaremos funciones simplificadas del MVP Enterprise anterior (idénticas).
# (En un entorno real, factorizaríamos en módulos.)

def page_inventario_productos():
    st.title("📦 Productos"); user = require_login(); is_admin = (user['role'] == 'admin')
    if is_admin:
        with st.expander("➕ Crear / editar / eliminar producto", expanded=False):
            mode = st.radio("Acción", ["Crear","Editar","Eliminar"], horizontal=True, key="prod_mode")
            categories = run_query("SELECT id, name FROM categories ORDER BY name"); suppliers = run_query("SELECT id, name FROM suppliers ORDER BY name")
            if mode == "Crear":
                sku = st.text_input("SKU *"); name = st.text_input("Nombre *"); brand = st.text_input("Marca"); model = st.text_input("Modelo")
                barcode = st.text_input("Código de barras"); uom = st.text_input("Unidad"); usage_type = st.selectbox("Tipo de uso *", ["Administrativo","Asistencial"], index=0)
                cat = st.selectbox("Categoría", [None] + categories['name'].tolist()); sup = st.selectbox("Proveedor", [None] + suppliers['name'].tolist())
                cost = st.number_input("Costo unitario", min_value=0.0, value=0.0, step=0.01); min_stock = st.number_input("Stock mínimo", min_value=0.0, value=0.0, step=1.0)
                active = st.checkbox("Activo", value=True)
                if st.button("Guardar producto"):
                    if not sku or not name: st.error("SKU y nombre son obligatorios.")
                    else:
                        cat_id = int(categories.loc[categories['name']==cat, 'id'].iloc[0]) if cat else None
                        sup_id = int(suppliers.loc[suppliers['name']==sup, 'id'].iloc[0]) if sup else None
                        try:
                            run_script("INSERT INTO products(sku,name,brand,model,barcode,uom,usage_type,category_id,supplier_id,unit_cost,min_stock,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (sku.strip(), name.strip(), brand.strip(), model.strip(), barcode.strip(), uom.strip(), usage_type, cat_id, sup_id, float(cost), float(min_stock), 1 if active else 0))
                            st.success("Producto creado.")
                        except sqlite3.IntegrityError: st.error("SKU duplicado.")
            elif mode == "Editar":
                df = df_products()
                if df.empty: st.info("No hay productos para editar.")
                else:
                    sel_name = st.selectbox("Selecciona producto", df['producto']); row = df[df['producto']==sel_name].iloc[0]
                    new_vals = {}
                    new_vals['sku'] = st.text_input("SKU *", value=row['sku']); new_vals['name'] = st.text_input("Nombre *", value=row['producto'])
                    new_vals['brand'] = st.text_input("Marca", value=row['marca'] or ""); new_vals['model'] = st.text_input("Modelo", value=row['modelo'] or "")
                    new_vals['barcode'] = st.text_input("Código barras", value=row['codigo_barras'] or ""); new_vals['uom'] = st.text_input("Unidad", value=row['unidad'] or "")
                    new_vals['usage_type'] = st.selectbox("Tipo de uso *", ["Administrativo","Asistencial"], index=(0 if (row.get('tipo','Administrativo')=='Administrativo') else 1))
                    categories = run_query("SELECT id,name FROM categories ORDER BY name"); suppliers = run_query("SELECT id,name FROM suppliers ORDER BY name")
                    new_cat = st.selectbox("Categoría", [None] + categories['name'].tolist(), index=(categories['name'].tolist().index(row['categoria'])+1 if pd.notna(row['categoria']) and row['categoria'] in categories['name'].tolist() else 0))
                    new_sup = st.selectbox("Proveedor", [None] + suppliers['name'].tolist(), index=(suppliers['name'].tolist().index(row['proveedor'])+1 if pd.notna(row['proveedor']) and row['proveedor'] in suppliers['name'].tolist() else 0))
                    new_cost = st.number_input("Costo unitario", min_value=0.0, value=float(row['costo_unit']), step=0.01); new_min = st.number_input("Stock mínimo", min_value=0.0, value=float(row['stock_min']), step=1.0)
                    active_flag = st.checkbox("Activo", value=bool(row['active']))
                    if st.button("Actualizar"):
                        cat_id = int(categories.loc[categories['name']==new_cat, 'id'].iloc[0]) if new_cat else None
                        sup_id = int(suppliers.loc[suppliers['name']==new_sup, 'id'].iloc[0]) if new_sup else None
                        run_script("UPDATE products SET sku=?, name=?, brand=?, model=?, barcode=?, uom=?, usage_type=?, category_id=?, supplier_id=?, unit_cost=?, min_stock=?, active=? WHERE id=?",
                                   (new_vals['sku'].strip(), new_vals['name'].strip(), new_vals['brand'].strip(), new_vals['model'].strip(), new_vals['barcode'].strip(), new_vals['uom'].strip(), new_vals['usage_type'], cat_id, sup_id, float(new_cost), float(new_min), 1 if active_flag else 0, int(row['id'])))
                        st.success("Producto actualizado.")
            else:
                df = df_products()
                if df.empty: st.info("No hay productos para eliminar.")
                else:
                    sel_name = st.selectbox("Selecciona producto a eliminar", df['producto'])
                    if st.button("Eliminar definitivamente ⚠️"):
                        pid = int(df[df['producto']==sel_name]['id'].iloc[0]); run_script("DELETE FROM products WHERE id=?", (pid,)); st.success("Producto eliminado.")
    st.subheader("Listado de productos"); df = df_products()
    filtro_tipo = st.selectbox("Filtrar por tipo", ["(Todos)","Administrativo","Asistencial"], index=0)
    if filtro_tipo != "(Todos)" and not df.empty: df = df[df['tipo']==filtro_tipo]
    st.dataframe(df, use_container_width=True)

def page_inventario_movimientos():
    st.title("🚚 Movimientos (kárdex)")
    user = require_login()
    if user['role'] not in ('admin','agente'): st.error("Solo administradores o agentes."); return
    products = run_query("SELECT id, name FROM products WHERE active=1 ORDER BY name")
    warehouses = run_query("SELECT id, name FROM warehouses ORDER BY name")
    with st.form("mov_form"):
        mtype = st.selectbox("Tipo", ["ENTRADA","SALIDA","TRANSFERENCIA","AJUSTE"], index=0)
        prod = st.selectbox("Producto", products['name'].tolist())
        c1,c2,c3 = st.columns(3)
        from_wh = None; to_wh = None
        if mtype in ("SALIDA","TRANSFERENCIA","AJUSTE"):
            from_wh = c1.selectbox("Desde bodega", warehouses['name'].tolist())
        if mtype in ("ENTRADA","TRANSFERENCIA"):
            to_wh = c2.selectbox("Hacia bodega", warehouses['name'].tolist())
        qty = c3.number_input("Cantidad", min_value=0.0, value=1.0, step=1.0)
        unit_cost = st.number_input("Costo unitario (si aplica)", min_value=0.0, value=0.0, step=0.01)
        reason = st.text_input("Motivo/Observación")
        submitted = st.form_submit_button("Registrar movimiento")
    if submitted:
        pid = int(products.loc[products['name']==prod,'id'].iloc[0])
        now = datetime.utcnow().isoformat()
        if mtype in ("ENTRADA","TRANSFERENCIA"): 
            if to_wh is None: st.error("Selecciona bodega destino."); return
            to_id = int(warehouses.loc[warehouses['name']==to_wh,'id'].iloc[0]); adjust_stock(pid, to_id, qty)
        if mtype in ("SALIDA","TRANSFERENCIA","AJUSTE"):
            if from_wh is None: st.error("Selecciona bodega origen."); return
            from_id = int(warehouses.loc[warehouses['name']==from_wh,'id'].iloc[0]); adjust_stock(pid, from_id, (-qty if mtype!="AJUSTE" else -qty))
        run_script("INSERT INTO movements(type,product_id,from_wh,to_wh,qty,unit_cost,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (mtype,pid, int(warehouses.loc[warehouses['name']==from_wh,'id'].iloc[0]) if from_wh else None, int(warehouses.loc[warehouses['name']==to_wh,'id'].iloc[0]) if to_wh else None, float(qty), float(unit_cost), reason, require_login()['id'], now))
        st.success("Movimiento registrado.")
    st.subheader("Kárdex reciente")
    mv = run_query("""SELECT m.created_at, m.type, p.name AS producto, w1.name AS desde, w2.name AS hacia, m.qty, m.unit_cost, m.reason, u.username AS usuario
                      FROM movements m
                      JOIN products p ON p.id=m.product_id
                      LEFT JOIN warehouses w1 ON w1.id=m.from_wh
                      LEFT JOIN warehouses w2 ON w2.id=m.to_wh
                      LEFT JOIN users u ON u.id=m.created_by
                      ORDER BY m.id DESC LIMIT 200""")
    st.dataframe(mv, use_container_width=True)

def page_cmdb():
    st.title("🗂️ CMDB – Elementos de Configuración (CI)")
    user = require_login()
    if user['role'] not in ('admin','agente'): st.error("Solo administradores o agentes."); return
    with st.form("ci_form"):
        name = st.text_input("Nombre del CI *"); ci_type = st.text_input("Tipo CI (Servidor, App, DB, Red, etc.) *")
        criticality = st.selectbox("Criticidad", ["Baja","Media","Alta","Crítica"], index=1)
        owner = st.text_input("Owner/Responsable"); location = st.text_input("Ubicación"); status = st.selectbox("Estado", ["Operativo","Mantenimiento","Baja","Asignado"], index=0)
        attrs = st.text_area("Atributos (JSON opcional)")
        submitted = st.form_submit_button("Guardar CI")
    if submitted:
        try: json.loads(attrs or "{}")
        except Exception: st.error("JSON inválido en atributos."); return
        run_script("INSERT INTO ci_items(name,ci_type,criticality,owner,location,status,attributes) VALUES(?,?,?,?,?,?,?)", (name.strip(), ci_type.strip(), criticality, owner.strip(), location.strip(), status, attrs or "{}"))
        st.success("CI creado.")
    st.subheader("Listado CI")
    df = run_query("SELECT id, name, ci_type, criticality, owner, location, status FROM ci_items ORDER BY id DESC")
    st.dataframe(df, use_container_width=True)
    st.subheader("Relaciones CI")
    cis = run_query("SELECT id, name FROM ci_items ORDER BY name")
    if not cis.empty:
        with st.form("ci_rel_form"):
            p = st.selectbox("CI Padre", cis['name'])
            c = st.selectbox("CI Hijo", cis['name'])
            r = st.text_input("Tipo de relación (depende de, usa, replica, etc.)")
            submitted2 = st.form_submit_button("Crear relación")
        if submitted2:
            pid = int(cis.loc[cis['name']==p,'id'].iloc[0])
            cid = int(cis.loc[cis['name']==c,'id'].iloc[0])
            if pid == cid:
                st.error("Padre y Hijo no pueden ser el mismo.")
            else:
                run_script("INSERT INTO ci_relations(parent_ci_id,child_ci_id,relation_type) VALUES(?,?,?)", (pid,cid,r.strip()))
                st.success("Relación creada.")

        rel = run_query("""SELECT pr.name AS padre, ch.name AS hijo, r.relation_type FROM ci_relations r
                           JOIN ci_items pr ON pr.id=r.parent_ci_id JOIN ci_items ch ON ch.id=r.child_ci_id ORDER BY pr.name, ch.name""")
st.dataframe(rel, use_container_width=True)

# --- UI: Nuevo Ticket ---
def page_tickets_nuevo():
    st.title("🎫 Nuevo Ticket"); user = require_login()
    products = run_query("SELECT id, name FROM products WHERE active=1 ORDER BY name")
    warehouses = run_query("SELECT id, name FROM warehouses ORDER BY name")
    assets = run_query("SELECT id, code || ' – ' || name AS label FROM assets WHERE active=1 ORDER BY code")
    cis = run_query("SELECT id, name FROM ci_items ORDER BY name")
    catalog = run_query("SELECT id, name, process, default_priority, policy_id, parent_id, owner_email FROM service_catalog ORDER BY name")
    problemas = run_query("SELECT id, code || ' – ' || title AS label FROM tickets WHERE itil_type='Problema' ORDER BY id DESC")
    with st.form("new_ticket"):
        cat_item = st.selectbox("Catálogo de servicios (opcional)", [None] + catalog['name'].tolist())
        itil_type = st.selectbox("Proceso ITIL", ["Incidente","Solicitud","Cambio","Problema"], index=0)
        title = st.text_input("Título *"); desc = st.text_area("Descripción")
        if title:
            kb = kb_search(title)
            if not kb.empty: st.info("Sugerencias de KB:"); st.dataframe(kb, use_container_width=True)
        ucols = st.columns(3)
        urgency = ucols[0].selectbox("Urgencia", ["Baja","Media","Alta","Crítica"], index=1)
        impact = ucols[1].selectbox("Impacto", ["Bajo","Medio","Alto","Crítico"], index=1)
        selected_catalog_id = int(catalog.loc[catalog['name']==cat_item,'id'].iloc[0]) if cat_item else None
        auto_pri = matrix_priority_service(selected_catalog_id, urgency, impact)
        prio = ucols[2].selectbox("Prioridad", ["Auto","Baja","Media","Alta","Crítica"], index=0)
        prio_value = (auto_pri if prio=="Auto" else prio)
        if cat_item:
            row = catalog.loc[catalog['name']==cat_item].iloc[0]
            itil_type = row['process']
            if prio == "Auto": prio_value = row['default_priority']
        response_sla, resolution_sla = sla_lookup(itil_type, prio_value)
        st.caption(f"SLA – 1ª respuesta: {response_sla}h · Resolución: {resolution_sla}h (prioridad {prio_value})")
        c1, c2 = st.columns(2)
        wh_name = c1.selectbox("Bodega relacionada", [None] + warehouses['name'].tolist())
        prod_name = c2.selectbox("Producto relacionado", [None] + products['name'].tolist())
        asset_label = st.selectbox("Activo relacionado", [None] + assets['label'].tolist())
        ci_name = st.selectbox("CI relacionado (CMDB)", [None] + cis['name'].tolist())
        change_risk = change_impact = planned_start = planned_end = backout_plan = None
        approval_mgr_status = "Pendiente"; approval_cab_status = "Pendiente"; approval_status = "Pendiente"
        problem_root_cause = problem_workaround = None; problem_parent_id = None
        if itil_type == "Cambio":
            st.markdown("**Datos de RFC (Cambio)**"); cc1, cc2 = st.columns(2)
            change_impact = cc2.selectbox("Impacto del cambio", ["Bajo","Medio","Alto"])
            # Riesgo automático preliminar (sin CI puede usarse heurística)
            _ci_id_tmp = int(cis.loc[cis['name']==ci_name,'id'].iloc[0]) if ci_name else None
            change_risk = auto_change_risk(_ci_id_tmp, change_impact, prio_value)
            st.info(f"Riesgo estimado (auto): **{change_risk}**")
            dp1, dp2 = st.columns(2); planned_start = dp1.date_input("Inicio planificado"); planned_end = dp2.date_input("Fin planificado")
            backout_plan = st.text_area("Plan de reversa")
        if itil_type == "Problema":
            st.markdown("**Datos de Problema**"); pc1, pc2 = st.columns(2)
            problem_root_cause = pc1.text_input("Causa raíz (si conocida)"); problem_workaround = pc2.text_input("Workaround (si existe)")
        if itil_type in ("Incidente","Solicitud"):
            if not problemas.empty:
                vinc = st.selectbox("Vincular a Problema", [None] + problemas['label'].tolist())
                if vinc: problem_parent_id = int(problemas.loc[problemas['label']==vinc,'id'].iloc[0])
        watchers = st.text_input("Correos watchers (coma)"); attachment = st.file_uploader("Adjunto (opcional)")
        submitted = st.form_submit_button("Crear Ticket")
    if submitted:
        if not title: st.error("El título es obligatorio."); return
        code = f"TCK-{datetime.utcnow().strftime('%Y%m%d')}-{int(run_query('SELECT COUNT(*) n FROM tickets WHERE DATE(created_at)=DATE(''now'')').loc[0,'n'])+1:04d}"
        now = datetime.utcnow(); due_res = now + timedelta(hours=int(resolution_sla))
        wh_id = int(warehouses.loc[warehouses['name']==wh_name,'id'].iloc[0]) if wh_name else None
        prod_id = int(products.loc[products['name']==prod_name,'id'].iloc[0]) if prod_name else None
        asset_id = int(assets.loc[assets['label']==asset_label,'id'].iloc[0]) if asset_label else None
        ci_id = int(cis.loc[cis['name']==ci_name,'id'].iloc[0]) if ci_name else None
        catalog_id = selected_catalog_id
        attach_path = None
        if attachment is not None:
            tdir = os.path.join(UPLOAD_DIR, code); os.makedirs(tdir, exist_ok=True)
            fpath = os.path.join(tdir, attachment.name); open(fpath,'wb').write(attachment.read()); attach_path = fpath
        run_script("""INSERT INTO tickets(code,title,description,category,priority,urgency,impact,status,sla_hours,response_sla_hours,created_by,assigned_to,
                      warehouse_id,product_id,asset_id,ci_id,catalog_id,watchers_emails,attachment_path,created_at,updated_at,due_at,itil_type,
                      change_risk,change_impact,planned_start,planned_end,approval_mgr_status,approval_cab_status,approval_status,backout_plan,
                      problem_root_cause,problem_workaround,problem_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (code, title.strip(), desc, itil_type, prio_value, urgency, impact, "Abierto", int(resolution_sla), int(response_sla), require_login()['id'], None,
                    wh_id, prod_id, asset_id, ci_id, catalog_id, watchers, attach_path, now.isoformat(), now.isoformat(), due_res.isoformat(), itil_type,
                    change_risk, change_impact, (planned_start.isoformat() if planned_start else None), (planned_end.isoformat() if planned_end else None),
                    approval_mgr_status, approval_cab_status, approval_status, backout_plan, problem_root_cause, problem_workaround, problem_parent_id))
        creator_email = run_query("SELECT email FROM users WHERE id=?", (require_login()['id'],)).loc[0,'email']
        t = {"code":code,"title":title,"priority":prio_value,"status":"Abierto","due_at":due_res.isoformat(),"itil_type":itil_type,"urgency":urgency,"impact":impact,"creator_email":creator_email,"watchers":watchers}
        extra = []
        # Aprobaciones multinivel (si Cambio)
        if itil_type == "Cambio":
            mgrs = [e.strip() for e in (get_setting("mgr_emails","") or "").split(",") if e.strip()]
            cabs = [e.strip() for e in (get_setting("cab_emails","") or "").split(",") if e.strip()]
            for em in mgrs: run_script("INSERT INTO change_approvals(ticket_id,level,approver_email,status) VALUES((SELECT id FROM tickets WHERE code=?),'Manager',?,'Pendiente')",(code, em))
            for em in cabs: run_script("INSERT INTO change_approvals(ticket_id,level,approver_email,status) VALUES((SELECT id FROM tickets WHERE code=?),'CAB',?,'Pendiente')",(code, em))
            # Áreas globales activas
            areas = run_query("SELECT name, emails FROM approval_areas WHERE active=1")
            for _, row in areas.iterrows():
                for em in [e.strip() for e in (row['emails'] or '').split(',') if e.strip()]:
                    run_script("INSERT INTO change_approvals(ticket_id,level,approver_email,status) VALUES((SELECT id FROM tickets WHERE code=?),?,?,'Pendiente')",(code, row['name'], em))
            # Áreas por servicio (si hay reglas)
            if catalog_id:
                rules = run_query("SELECT area_name FROM service_approval_rules WHERE catalog_id=?", (catalog_id,))
                if not rules.empty:
                    for _, r in rules.iterrows():
                        emails = run_query("SELECT emails FROM approval_areas WHERE name=? AND active=1", (r['area_name'],))
                        if not emails.empty:
                            for em in [e.strip() for e in (emails.loc[0,'emails'] or '').split(',') if e.strip()]:
                                run_script("INSERT INTO change_approvals(ticket_id,level,approver_email,status) VALUES((SELECT id FROM tickets WHERE code=?),?,?,'Pendiente')",(code, r['area_name'], em))
            notify_approval_requested(t, "Manager"); notify_approval_requested(t, "CAB")
            extra = mgrs + cabs
        notify_ticket_created(t, creator_email, None, [e.strip() for e in (watchers or '').split(',') if e.strip()], extra=extra)
        st.success(f"Ticket {code} creado correctamente.")

# --- UI: Bandejas, Kanban, Aprobaciones, Reportes/Encuestas (similar a Enterprise, con ampliaciones CES/NPS y equipo) ---
def page_tickets_bandeja(mis=False, equipo=False):
    user = require_login(); role = user['role']
    st.title("📥 Bandeja de Tickets" if not mis and not equipo else ("📌 Mis Tickets" if mis else "👥 Tickets de mi Equipo"))
    col1, col2, col3, col4 = st.columns(4)
    estado = col1.multiselect("Estado", ["Abierto","En Progreso","Resuelto","Cerrado"], default=["Abierto","En Progreso"])
    prio = col2.multiselect("Prioridad", ["Baja","Media","Alta","Crítica"], default=["Media","Alta","Crítica"])
    texto = col3.text_input("Buscar (título/desc)"); ver_vencidos = col4.checkbox("Solo vencidos")
    sql = """SELECT t.*, u.username as creador, a.username as asignado, u.email as creador_email, a.email as asignado_email
             FROM tickets t LEFT JOIN users u ON u.id=t.created_by LEFT JOIN users a ON a.id=t.assigned_to WHERE 1=1"""
    params = []
    if estado: sql += f" AND t.status IN ({','.join(['?']*len(estado))})"; params += estado
    if prio: sql += f" AND t.priority IN ({','.join(['?']*len(prio))})"; params += prio
    if texto: sql += " AND (t.title LIKE ? OR t.description LIKE ?)"; params += [f"%{texto}%", f"%{texto}%"]
    if ver_vencidos: sql += " AND t.due_at IS NOT NULL AND t.due_at < ? AND t.status IN ('Abierto','En Progreso')"; params += [datetime.utcnow().isoformat()]
    if mis: sql += " AND (t.created_by=? OR t.assigned_to=?)"; params += [user['id'], user['id']]
    if equipo:
        # usuarios en mis equipos
        ut = run_query("""SELECT u.id FROM user_teams ut JOIN users u ON u.id=ut.user_id 
                          WHERE ut.team_id IN (SELECT team_id FROM user_teams WHERE user_id=?)""", (user['id'],))
        ids = ut['id'].tolist() if not ut.empty else [user['id']]
        sql += f" AND (t.assigned_to IN ({','.join(['?']*len(ids))}) OR t.created_by IN ({','.join(['?']*len(ids))}))"; params += ids + ids
    if role == 'visor' and not mis: sql += " AND t.created_by=?"; params += [user['id']]
    sql += " ORDER BY t.updated_at DESC LIMIT 500"
    df = run_query(sql, tuple(params))
    if df.empty: st.info("No hay tickets que cumplan el filtro."); return
    st.dataframe(df[["code","itil_type","title","urgency","impact","priority","status","due_at","creador","asignado","updated_at"]], use_container_width=True)
    st.divider(); st.subheader("Gestionar ticket")
    sel = st.selectbox("Selecciona ticket", df['code'].tolist()); 
    if not sel: return
    t = df[df['code']==sel].iloc[0]
    st.markdown(f"**{t['title']}**"); st.caption(f"ITIL: {t.get('itil_type','Incidente')} · Estado: {t['status']} · Urgencia: {t.get('urgency')} · Impacto: {t.get('impact')} · Prioridad: {t['priority']} · SLA resp: {t.get('response_sla_hours',4)}h · SLA res: {t['sla_hours']}h · Vence: {t['due_at']}")
    st.write(t['description'] or "(Sin descripción)")
    is_admin = (role == 'admin'); is_agent = (role == 'agente')
    assigned_to_me = (pd.notna(t['assigned_to']) and int(t['assigned_to']) == user['id'])
    unassigned = pd.isna(t['assigned_to']) or (str(t['assigned_to']).strip() == "")
    can_edit_status = is_admin or (is_agent and assigned_to_me); can_adjust_sla = is_admin or (is_agent and assigned_to_me)
    can_update_watchers = can_edit_status; can_assign_admin = is_admin; can_self_assign = is_agent and (unassigned or assigned_to_me)
    can_add_comment = is_admin or is_agent or (pd.notna(t['created_by']) and int(t['created_by']) == user['id'])
    users_df = run_query("SELECT id, username, email FROM users WHERE active=1 ORDER BY username")
    colA, colB, colC = st.columns(3)
    opts = ["Abierto","En Progreso","Resuelto","Cerrado"]
    if can_edit_status: new_status = colA.selectbox("Cambiar estado", opts, index=opts.index(t['status']))
    else: colA.text_input("Estado", value=t['status'], disabled=True); new_status = t['status']
    assignee = t['asignado'] if pd.notna(t['asignado']) else None
    assignee_email = t.get('asignado_email', None) if pd.notna(t.get('asignado_email', None)) else None
    if can_assign_admin:
        assignee = colB.selectbox("Asignar a", [None] + users_df['username'].tolist(), index=(users_df['username'].tolist().index(t['asignado'])+1 if pd.notna(t['asignado']) and t['asignado'] in users_df['username'].tolist() else 0))
        assignee_email = str(users_df.loc[users_df['username']==assignee,'email'].iloc[0]) if assignee else None
    elif can_self_assign:
        options = [None, user['username']]; default_index = options.index(t['asignado']) if pd.notna(t['asignado']) and t['asignado'] in options else 0
        assignee = colB.selectbox("Asignar a (solo tú)", options, index=default_index); assignee_email = user['email'] if assignee == user['username'] else None
    else:
        colB.text_input("Asignado a", value=(t['asignado'] or ""), disabled=True)
    more_hours = colC.number_input("Ajustar SLA resolución (+h)", min_value=0, value=0, disabled=not can_adjust_sla)
    watchers = st.text_input("Watchers (coma)", value=t.get('watchers_emails','') or '', disabled=not can_update_watchers)
    itil_type = t.get('itil_type','Incidente')
    risk=impactchg=ps=pe=backout=root_cause=workaround=None
    if itil_type == "Cambio":
        st.markdown("**RFC (Cambio)**"); c1, c2 = st.columns(2)
        risk = c1.selectbox("Riesgo", ["Bajo","Medio","Alto"], index=(["Bajo","Medio","Alto"].index(t.get('change_risk','Bajo')) if t.get('change_risk') in ["Bajo","Medio","Alto"] else 0), disabled=False)
        impactchg = c2.selectbox("Impacto del cambio", ["Bajo","Medio","Alto"], index=(["Bajo","Medio","Alto"].index(t.get('change_impact','Medio')) if t.get('change_impact') in ["Bajo","Medio","Alto"] else 1), disabled=False)
        d1, d2 = st.columns(2)
        ps = d1.text_input("Inicio planificado (YYYY-MM-DD)", value=(t.get('planned_start') or ""))
        pe = d2.text_input("Fin planificado (YYYY-MM-DD)", value=(t.get('planned_end') or ""))
        backout = st.text_area("Plan de reversa", value=t.get('backout_plan') or "")
    if itil_type == "Problema":
        st.markdown("**Análisis de Problema**"); pc1, pc2 = st.columns(2)
        root_cause = pc1.text_input("Causa raíz", value=t.get('problem_root_cause') or "")
        workaround = pc2.text_input("Workaround", value=t.get('problem_workaround') or "")
    if can_edit_status and not t.get('first_response_at'):
        if st.button("✅ Marcar 1ª respuesta ahora"):
            now = datetime.utcnow().isoformat(); run_script("UPDATE tickets SET first_response_at=?, updated_at=? WHERE id=?", (now, now, int(t['id']))); st.success("Primera respuesta registrada."); st.rerun()
    else:
        if t.get('first_response_at'): st.info(f"1ª respuesta: {t['first_response_at']}")
    comment = st.text_area("Agregar comentario", disabled=not can_add_comment)
    if (can_edit_status or can_assign_admin or can_self_assign or can_adjust_sla) and st.button("Guardar cambios"):
        if not state_transition_ok(t['status'], new_status) and role != 'admin':
            st.error("Transición de estado no permitida por el flujo ITIL."); return
        if itil_type == "Cambio" and new_status in ("Resuelto","Cerrado") and role != 'admin':
            # Deben existir aprobaciones y estar en Aprobado
            ap = run_query("SELECT status FROM change_approvals WHERE ticket_id=?", (int(t['id']),))
            if ap.empty or any(s != "Aprobado" for s in ap['status'].tolist()):
                st.error("No puedes resolver/cerrar un Cambio sin todas las aprobaciones requeridas."); return
        now = datetime.utcnow(); assignee_id=None; assignee_email_eff=None; assignee_name_eff=None
        if assignee:
            if assignee == user['username']: assignee_id=user['id']; assignee_email_eff=user['email']; assignee_name_eff=assignee
            elif assignee in users_df['username'].tolist():
                assignee_id=int(users_df.loc[users_df['username']==assignee,'id'].iloc[0]); assignee_email_eff=str(users_df.loc[users_df['username']==assignee,'email'].iloc[0]); assignee_name_eff=assignee
        due_at = t['due_at']
        if more_hours > 0 and can_adjust_sla:
            try: due_at = (datetime.fromisoformat(due_at) + timedelta(hours=int(more_hours))).isoformat() if due_at else (now + timedelta(hours=int(more_hours))).isoformat()
            except Exception: due_at = (now + timedelta(hours=int(more_hours))).isoformat()
        closed_at = t['closed_at']; resolved_at = t.get('resolved_at'); old_status = t['status']
        if new_status in ("Resuelto","Cerrado") and can_edit_status:
            resolved_at = now.isoformat()
            if new_status == "Cerrado": closed_at = now.isoformat()
        run_script("""UPDATE tickets SET status=?, assigned_to=?, updated_at=?, due_at=?, closed_at=?, watchers_emails=?,
                                       change_risk=?, change_impact=?, planned_start=?, planned_end=?, backout_plan=?,
                                       problem_root_cause=?, problem_workaround=?, resolved_at=? WHERE id=?""",
                   (new_status, assignee_id, now.isoformat(), due_at, closed_at, watchers if can_update_watchers else t.get('watchers_emails',''), risk, impactchg, ps, pe, backout, root_cause, workaround, resolved_at, int(t['id'])))
        if comment and can_add_comment:
            run_script("INSERT INTO ticket_comments(ticket_id,author_id,comment,created_at) VALUES(?,?,?,?)", (int(t['id']), user['id'], comment, now.isoformat()))
        creator_email = t.get('creador_email',''); watchers_list = [e.strip() for e in (watchers or '').split(',') if e.strip()]
        T = {"code":t['code'],"title":t['title'],"priority":t['priority'],"status":new_status,"due_at":due_at,"assignee_name":assignee or t.get('asignado'),"itil_type":t.get('itil_type','Incidente'),"urgency":t.get('urgency'),"impact":t.get('impact'),"creator_email":creator_email,"assignee_email": assignee_email}
        if assignee: notify_ticket_assigned(T, creator_email, assignee_email_eff, watchers_list)
        if new_status != old_status: notify_ticket_status_change(T, creator_email, assignee_email_eff or t.get('asignado_email'), watchers_list, old_status)
        if comment: notify_ticket_commented(T, creator_email, assignee_email_eff or t.get('asignado_email'), watchers_list, (comment[:140] + ("…" if len(comment)>140 else "")))
        if new_status == "Resuelto": notify_ticket_resolved(T, creator_email, watchers_list)
        st.success("Ticket actualizado."); st.rerun()

def page_kanban_agente():
    st.title("🧰 Kanban del Agente")
    user = require_login()
    if user['role'] != 'agente': st.error("Solo agentes."); return
    df = run_query("SELECT id, code, title, status, priority FROM tickets WHERE assigned_to=? ORDER BY updated_at DESC LIMIT 300", (user['id'],))
    if df.empty: st.info("No tienes tickets asignados."); return
    cols = st.columns(3)
    for idx, estado in enumerate(["Abierto","En Progreso","Resuelto"]):
        with cols[idx]:
            st.subheader(estado)
            sub = df[df['status']==estado]
            for _, row in sub.iterrows():
                st.write(f"**{row['code']}** – {row['title']} ({row['priority']})")
                c1, c2 = st.columns(2)
                if estado != "Abierto" and c1.button("⬅️ Atrás", key=f"b{row['id']}"):
                    run_script("UPDATE tickets SET status=?, updated_at=? WHERE id=?", ("Abierto" if estado=="En Progreso" else "En Progreso", datetime.utcnow().isoformat(), int(row['id']))); st.rerun()
                if estado != "Resuelto" and c2.button("➡️ Avanzar", key=f"f{row['id']}"):
                    run_script("UPDATE tickets SET status=?, updated_at=? WHERE id=?", ("En Progreso" if estado=="Abierto" else "Resuelto", datetime.utcnow().isoformat(), int(row['id']))); st.rerun()

def page_aprobaciones():
    st.title("✅ Centro de Aprobaciones")
    user = require_login()
    my_email = user.get('email','') or ""
    df = run_query("""SELECT ca.id, t.code, t.title, ca.level, ca.status, ca.approver, ca.approver_email
                      FROM change_approvals ca JOIN tickets t ON t.id=ca.ticket_id
                      WHERE ca.status='Pendiente' AND (?='admin' OR ca.approver_email=?)""", (user['role'], my_email))
    st.dataframe(df, use_container_width=True)
    if df.empty: st.info("No tienes aprobaciones pendientes."); return
    sel = st.selectbox("Selecciona aprobación", df['id'].astype(str).tolist())
    if not sel: return
    row = df[df['id']==int(sel)].iloc[0]
    st.write(f"Ticket **{row['code']}** – {row['title']} · Nivel: **{row['level']}**")
    c1, c2 = st.columns(2)
    approver_name = c1.text_input("Tu nombre", value=user['username'])
    decision = c2.selectbox("Decisión", ["Aprobado","Rechazado"], index=0)
    comment = st.text_area("Comentario (opcional)")
    if st.button("Enviar decisión"):
        run_script("UPDATE change_approvals SET status=?, decided_at=?, approver=?, comment=? WHERE id=?", (decision, datetime.utcnow().isoformat(), approver_name, comment, int(sel)))
        tdf = run_query("""SELECT t.id, t.code, t.title,
                                  MIN(CASE WHEN level='Manager' THEN status END) mgr, MIN(CASE WHEN level='CAB' THEN status END) cab,
                                  t.watchers_emails, u.email as creator_email, a.email as assignee_email
                           FROM change_approvals ca JOIN tickets t ON t.id=ca.ticket_id
                           LEFT JOIN users u ON u.id=t.created_by LEFT JOIN users a ON a.id=t.assigned_to
                           WHERE t.code=? GROUP BY t.id""", (row['code'],))
        TT = tdf.iloc[0]; mgr = TT['mgr']; cab = TT['cab']
        overall = "Pendiente"
        # si alguna es Rechazado => Rechazado; si todas las filas de approvals (incl áreas) son Aprobado => Aprobado
        ap_all = run_query("SELECT status FROM change_approvals WHERE ticket_id=?", (int(TT['id']),))
        if any(x=="Rechazado" for x in ap_all['status'].tolist()): overall = "Rechazado"
        elif all(x=="Aprobado" for x in ap_all['status'].tolist()): overall = "Aprobado"
        run_script("UPDATE tickets SET approval_mgr_status=?, approval_cab_status=?, approval_status=?, updated_at=? WHERE id=?", (mgr or "Pendiente", cab or "Pendiente", overall, datetime.utcnow().isoformat(), int(TT['id'])))
        T = {"code":row['code'], "title":row['title'], "status":overall, "assignee_email":TT.get('assignee_email'), "creator_email":TT.get('creator_email'), "watchers":TT.get('watchers_emails','')}
        notify_approval_decided(T, row['level'], decision, approver_name)
        st.success("Decisión registrada."); st.rerun()

def page_reportes_export():
    user = require_login()
    if user['role'] != 'admin': st.error("Solo administradores."); return
    st.title("📤 Reportes / Encuestas / Exportar")
    inv = df_stock(); st.dataframe(inv, use_container_width=True); st.download_button("⬇️ CSV Inventario", inv.to_csv(index=False).encode('utf-8'), file_name="inventario.csv", mime="text/csv")
    t = run_query("SELECT * FROM tickets ORDER BY updated_at DESC"); st.dataframe(t, use_container_width=True)
    if not t.empty:
        def in_sla_first(row):
            try:
                if pd.isna(row['first_response_at']): return False
                created = datetime.fromisoformat(row['created_at']); first = datetime.fromisoformat(row['first_response_at'])
                return (first - created) <= timedelta(hours=int(row.get('response_sla_hours',4) or 4))
            except Exception: return False
        def in_sla_resolution(row):
            try:
                if pd.isna(row['resolved_at']): return False
                created = datetime.fromisoformat(row['created_at']); resolved = datetime.fromisoformat(row['resolved_at'])
                return (resolved - created) <= timedelta(hours=int(row['sla_hours']))
            except Exception: return False
        t['ok_first'] = t.apply(in_sla_first, axis=1); t['ok_res'] = t.apply(in_sla_resolution, axis=1)
        first_ratio = 100.0 * (t['ok_first'].sum() / max(1, t['ok_first'].count())); res_ratio = 100.0 * (t['ok_res'].sum() / max(1, t['ok_res'].count()))
        tt = t[~t['resolved_at'].isna()].copy(); mttr_h = tt.apply(lambda r: (datetime.fromisoformat(r['resolved_at']) - datetime.fromisoformat(r['created_at'])).total_seconds()/3600.0, axis=1).mean() if not tt.empty else 0.0
        c1,c2,c3 = st.columns(3); c1.metric("Cumplimiento 1ª respuesta", f"{first_ratio:,.1f}%"); c2.metric("Cumplimiento resolución", f"{res_ratio:,.1f}%"); c3.metric("MTTR (h)", f"{mttr_h:,.1f}")
        grp = run_query("SELECT itil_type as proceso, status, COUNT(*) n FROM tickets GROUP BY itil_type, status")
        if not grp.empty:
            chart = alt.Chart(grp).mark_bar().encode(x='proceso:N', y='n:Q', color='status:N', tooltip=['proceso','status','n']).properties(height=300)
            st.altair_chart(chart, use_container_width=True)
    # CSAT/CES/NPS
    st.subheader("📈 Encuestas")
    s_csat = run_query("""SELECT t.code, u.username, s.rating, s.comment, s.created_at
                          FROM ticket_surveys s JOIN tickets t ON t.id=s.ticket_id LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC""")
    st.write("**CSAT** (1–5):")
    st.dataframe(s_csat, use_container_width=True)
    if not s_csat.empty: st.metric("CSAT promedio", f"{s_csat['rating'].mean():,.2f} / 5")
    s_ces = run_query("""SELECT t.code, u.username, s.score, s.comment, s.created_at
                         FROM surveys_ces s LEFT JOIN tickets t ON t.id=s.ticket_id LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC""")
    st.write("**CES** (1–7, menor=menos esfuerzo):")
    st.dataframe(s_ces, use_container_width=True)
    if not s_ces.empty: st.metric("CES promedio", f"{s_ces['score'].mean():,.2f} / 7")
    s_nps = run_query("""SELECT u.username, s.score, s.comment, s.created_at FROM surveys_nps s LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC""")
    st.write("**NPS** (0–10, promotores >=9):")
    st.dataframe(s_nps, use_container_width=True)
    if not s_nps.empty:
        promoters = (s_nps['score']>=9).sum(); detractors = (s_nps['score']<=6).sum(); total = max(1,len(s_nps))
        nps = 100.0*(promoters/total) - 100.0*(detractors/total); st.metric("NPS", f"{nps:,.1f}")

    import io; excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine='xlsxwriter') as w: inv.to_excel(w, sheet_name='inventario', index=False); t.to_excel(w, sheet_name='tickets', index=False); s_csat.to_excel(w, sheet_name='csat', index=False); s_ces.to_excel(w, sheet_name='ces', index=False); s_nps.to_excel(w, sheet_name='nps', index=False)
    st.download_button("⬇️ XLSX Inventario+Tickets+Encuestas", excel_buf.getvalue(), file_name="reportes_enterprise_plus.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def page_kb():
    st.title("📚 Base de Conocimiento (KB)")
    user = require_login()
    q = st.text_input("Buscar artículos por título/tags")
    df = run_query("SELECT code, title, tags FROM kb_articles WHERE title LIKE ? OR tags LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%"))
    st.dataframe(df, use_container_width=True)
    if user['role'] in ('admin','agente'):
        st.subheader("Crear/Actualizar artículo")
        with st.form("kb_form"):
            code = st.text_input("Código (único)", value=f"KB-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}")
            title = st.text_input("Título *"); tags = st.text_input("Tags (coma)")
            body = st.text_area("Contenido (Markdown) *", height=200)
            submit = st.form_submit_button("Guardar")
        if submit:
            now = datetime.utcnow().isoformat()
            try:
                run_script("INSERT INTO kb_articles(code,title,body,tags,created_by,created_at) VALUES(?,?,?,?,?,?)", (code.strip(), title.strip(), body, tags, user['id'], now))
                st.success("Artículo creado.")
            except sqlite3.IntegrityError:
                run_script("UPDATE kb_articles SET title=?, body=?, tags=? WHERE code=?", (title.strip(), body, tags, code.strip())); st.success("Artículo actualizado.")

def page_config():
    st.title("⚙️ Configuración – Notificaciones / SLA / Matrices / Catálogo / Aprobaciones / Webhooks / SSO")
    if require_login()['role'] != 'admin': st.error("Solo administradores."); return
    st.info("Sugerencia: define la clave SMTP por variable de entorno **APP_SMTP_PASSWORD**. El SSO usa `APP_SSO_SECRET`.")
    with st.form("smtp_form"):
        server = st.text_input("Servidor SMTP", value=get_setting("smtp_server","")); port = st.number_input("Puerto", min_value=1, value=int(get_setting("smtp_port","587") or 587))
        use_tls = st.checkbox("Usar TLS", value=get_setting("smtp_use_tls","1")=="1"); username = st.text_input("Usuario SMTP", value=get_setting("smtp_username",""))
        password = st.text_input("Contraseña SMTP (opcional)", type="password", value=get_setting("smtp_password","")); from_addr = st.text_input("Remitente (From)", value=get_setting("smtp_from", username or ""))
        notif_create = st.checkbox("Notificar en creación", value=get_setting("notif_on_create","1")=="1")
        notif_resolve = st.checkbox("Notificar en resolución", value=get_setting("notif_on_resolve","1")=="1")
        notif_change = st.checkbox("Notificar en cada cambio de estado", value=get_setting("notif_on_status_change","1")=="1")
        notif_comment = st.checkbox("Notificar en nuevos comentarios", value=get_setting("notif_on_comment","1")=="1")
        notif_appr = st.checkbox("Notificar aprobaciones (solicitud/decisión)", value=get_setting("notif_on_approval","1")=="1")
        default_to = st.text_input("Correos por defecto (coma)", value=get_setting("notif_default_to",""))
        mgr_emails = st.text_input("Correos Manager (coma)", value=get_setting("mgr_emails",""))
        cab_emails = st.text_input("Correos CAB (coma)", value=get_setting("cab_emails",""))
        webhook_url = st.text_input("Webhook URL (POST JSON)", value=get_setting("webhook_url",""))
        webhook_token = st.text_input("Webhook Bearer token (opcional)", value=get_setting("webhook_token",""))
        submitted = st.form_submit_button("Guardar notificaciones/webhooks")
    if submitted:
        for k,v in [("smtp_server",server.strip()),("smtp_port",str(int(port))),("smtp_use_tls","1" if use_tls else "0"),("smtp_username",username.strip()),("smtp_from",from_addr.strip()),("notif_on_create","1" if notif_create else "0"),("notif_on_resolve","1" if notif_resolve else "0"),("notif_on_status_change","1" if notif_change else "0"),("notif_on_comment","1" if notif_comment else "0"),("notif_on_approval","1" if notif_appr else "0"),("notif_default_to",default_to.strip()),("mgr_emails",mgr_emails.strip()),("cab_emails",cab_emails.strip()),("webhook_url",webhook_url.strip()),("webhook_token",webhook_token.strip())]:
            set_setting(k,v)
        if password: set_setting("smtp_password", password)
        st.success("Configuración guardada.")
    st.divider()
    # SLA
    st.subheader("Políticas de SLA")
    pol = run_query("SELECT id, process, priority, response_hours, resolution_hours, active FROM sla_policies ORDER BY process, priority")
    st.dataframe(pol, use_container_width=True)
    with st.form("add_sla"):
        c1,c2,c3,c4 = st.columns(4)
        p = c1.selectbox("Proceso", ["Incidente","Solicitud","Cambio","Problema"])
        pr = c2.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"])
        rh = c3.number_input("1ª respuesta (h)", min_value=1, value=4); rs = c4.number_input("Resolución (h)", min_value=1, value=48)
        add = st.form_submit_button("Agregar política")
    if add:
        run_script("INSERT INTO sla_policies(process,priority,response_hours,resolution_hours,active) VALUES(?,?,?,?,1)", (p,pr,int(rh),int(rs)))
        st.success("Política agregada."); st.rerun()
    # Matriz global
    st.subheader("Matriz Urgencia×Impacto → Prioridad (Global)")
    mat = run_query("SELECT urgency, impact, priority FROM priority_matrix ORDER BY urgency, impact")
    st.dataframe(mat, use_container_width=True)
    with st.form("add_mat"):
        u = st.selectbox("Urgencia", ["Baja","Media","Alta","Crítica"], index=1)
        i = st.selectbox("Impacto", ["Bajo","Medio","Alto","Crítico"], index=1)
        pr = st.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"], index=1)
        addm = st.form_submit_button("Guardar combinación global")
    if addm:
        try:
            run_script("INSERT INTO priority_matrix(urgency,impact,priority) VALUES(?,?,?)", (u,i,pr)); st.success("Combinación agregada.")
        except sqlite3.IntegrityError:
            run_script("UPDATE priority_matrix SET priority=? WHERE urgency=? AND impact=?", (pr,u,i)); st.success("Combinación actualizada.")
        st.rerun()
    # Matriz por servicio
    st.subheader("Matriz por Servicio (catálogo)")
    cat = run_query("SELECT id, name FROM service_catalog ORDER BY name")
    st.dataframe(run_query("""SELECT spm.catalog_id, sc.name AS servicio, spm.urgency, spm.impact, spm.priority
                              FROM service_priority_matrix spm JOIN service_catalog sc ON sc.id=spm.catalog_id
                              ORDER BY sc.name, spm.urgency, spm.impact"""), use_container_width=True)
    if not cat.empty:
        with st.form("add_spm"):
            serv = st.selectbox("Servicio", cat['name'])
            u = st.selectbox("Urgencia (svc)", ["Baja","Media","Alta","Crítica"], index=1)
            i = st.selectbox("Impacto (svc)", ["Bajo","Medio","Alto","Crítico"], index=1)
            pr = st.selectbox("Prioridad (svc)", ["Baja","Media","Alta","Crítica"], index=1)
            save = st.form_submit_button("Guardar combinación por servicio")
        if save:
            cid = int(cat.loc[cat['name']==serv,'id'].iloc[0])
            try:
                run_script("INSERT INTO service_priority_matrix(catalog_id,urgency,impact,priority) VALUES(?,?,?,?)",(cid,u,i,pr)); st.success("Matriz por servicio agregada.")
            except sqlite3.IntegrityError:
                run_script("UPDATE service_priority_matrix SET priority=? WHERE catalog_id=? AND urgency=? AND impact=?", (pr,cid,u,i)); st.success("Matriz por servicio actualizada.")
            st.rerun()
    # Catálogo
    st.subheader("Catálogo de Servicios (jerárquico)")
    cat2 = run_query("SELECT id, parent_id, name, process, default_priority, policy_id, owner_email FROM service_catalog ORDER BY name")
    st.dataframe(cat2, use_container_width=True)
    with st.form("add_cat"):
        par = st.selectbox("Padre (opcional)", [None] + cat2['name'].tolist())
        n = st.text_input("Nombre del servicio"); p2 = st.selectbox("Proceso", ["Incidente","Solicitud","Cambio","Problema"])
        dp = st.selectbox("Prioridad por defecto", ["Baja","Media","Alta","Crítica"], index=1)
        pols = run_query("SELECT id, process, priority FROM sla_policies ORDER BY process, priority")
        pid = st.selectbox("Política SLA (opcional)", [None] + pols['id'].astype(str).tolist())
        owner = st.text_input("Owner email (opcional)")
        add2 = st.form_submit_button("Agregar servicio")
    if add2:
        parent_id = int(cat2.loc[cat2['name']==par,'id'].iloc[0]) if par else None
        run_script("INSERT INTO service_catalog(parent_id,name,process,default_priority,policy_id,owner_email) VALUES(?,?,?,?,?,?)", (parent_id, n.strip(), p2, dp, int(pid) if pid else None, owner.strip() or None)); st.success("Servicio agregado."); st.rerun()
    # Aprobaciones por áreas
    st.subheader("Aprobaciones por Áreas")
    areas = run_query("SELECT id, name, emails, active FROM approval_areas ORDER BY name")
    st.dataframe(areas, use_container_width=True)
    with st.form("add_area"):
        an = st.text_input("Nombre área"); aemails = st.text_input("Emails (coma)"); aact = st.checkbox("Activa", value=True)
        adda = st.form_submit_button("Guardar área")
    if adda:
        try:
            run_script("INSERT INTO approval_areas(name,emails,active) VALUES(?,?,?)", (an.strip(), aemails.strip(), 1 if aact else 0)); st.success("Área agregada.")
        except sqlite3.IntegrityError:
            run_script("UPDATE approval_areas SET emails=?, active=? WHERE name=?", (aemails.strip(), 1 if aact else 0, an.strip())); st.success("Área actualizada.")
        st.rerun()
    if not cat2.empty and not areas.empty:
        with st.form("svc_area_rule"):
            svc = st.selectbox("Servicio", cat2['name']); area = st.selectbox("Área", areas['name'])
            required = st.checkbox("Requerida", value=True)
            saver = st.form_submit_button("Guardar regla de aprobación por servicio")
        if saver:
            cid = int(cat2.loc[cat2['name']==svc,'id'].iloc[0])
            try:
                run_script("INSERT INTO service_approval_rules(catalog_id,area_name,required) VALUES(?,?,?)", (cid, area, 1 if required else 0)); st.success("Regla agregada.")
            except sqlite3.IntegrityError:
                run_script("UPDATE service_approval_rules SET required=? WHERE catalog_id=? AND area_name=?", (1 if required else 0, cid, area)); st.success("Regla actualizada.")
            st.rerun()

# --- Sidebar y routing ---
def sidebar_menu():
    user = require_login(); st.sidebar.title("Navegación"); st.sidebar.write(f"👤 **{user['username']}** · Rol: *{user['role']}*")
    role = user['role']
    if role == 'admin':
        items = ["Dashboard","Inventario – Productos","Inventario – Movimientos","CMDB","Tickets – Nuevo","Tickets – Bandeja","Tickets – Mis Tickets","Tickets – Equipo","Kanban (Agente)","Base de Conocimiento","Reportes / Exportar","Centro de Aprobaciones","Configuración"]
    elif role == 'agente':
        items = ["Dashboard","Inventario – Movimientos","CMDB","Tickets – Nuevo","Tickets – Bandeja","Tickets – Mis Tickets","Tickets – Equipo","Kanban (Agente)","Base de Conocimiento","Centro de Aprobaciones"]
    else:
        items = ["Dashboard","Tickets – Nuevo","Tickets – Mis Tickets","Base de Conocimiento"]
    menu = st.sidebar.radio("Ir a", items, index=0)
    if st.sidebar.button("Cerrar sesión"): st.session_state.pop(SESSION_USER_KEY, None); st.rerun()
    return menu

def router():
    user = require_login()
    if not user: login_ui(); return
    page = sidebar_menu()
    if page == "Dashboard": page_dashboard()
    elif page == "Inventario – Productos": page_inventario_productos()
    elif page == "Inventario – Movimientos": page_inventario_movimientos()
    elif page == "CMDB": page_cmdb()
    elif page == "Tickets – Nuevo": page_tickets_nuevo()
    elif page == "Tickets – Bandeja": page_tickets_bandeja(mis=False)
    elif page == "Tickets – Mis Tickets": page_tickets_bandeja(mis=True)
    elif page == "Tickets – Equipo": page_tickets_bandeja(mis=False, equipo=True)
    elif page == "Kanban (Agente)": page_kanban_agente()
    elif page == "Base de Conocimiento": page_kb()
    elif page == "Reportes / Exportar": page_reportes_export()
    elif page == "Centro de Aprobaciones": page_aprobaciones()
    elif page == "Configuración": page_config()

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="🧭", layout="wide")
    init_db()
    try_sso_login()
    if not require_login():
        login_ui(); return
    router()

if __name__ == "__main__": main()
