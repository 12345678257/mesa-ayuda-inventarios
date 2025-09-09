# app_mesa_ayuda_inventarios_streamlit.py
# (archivo completo con ITIL, notificaciones por estado, recuperar/cambiar contraseña, etc.)
# Para detalles ver README.md; usuario inicial: admin/admin
import os, io, re, secrets, hashlib, sqlite3
from datetime import datetime, timedelta
from typing import Optional, List
import pandas as pd
import streamlit as st
import altair as alt

APP_TITLE = "Mesa de Ayuda e Inventarios"
DB_PATH = os.environ.get("APP_DB_PATH") or os.path.join(os.path.dirname(__file__), "inventarios_helpdesk.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

def conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def _retry_migrating(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if "no such column" in str(e) or "has no column" in str(e) or "no such table" in str(e):
            migrate_schema()
            return fn(*args, **kwargs)
        raise

def run_script(sql, params=()):
    def _exec():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            cx.execute(sql, params)
            cx.commit()
    return _retry_migrating(_exec)

def run_query(sql, params=()):
    def _q():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            return pd.read_sql_query(sql, cx, params=params)
    return _retry_migrating(_q)

def hash_password(password: str, salt: str) -> str:
    import hashlib
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

INIT_SQL = r"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','agente','visor')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS warehouses (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, location TEXT);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, contact TEXT, email TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    brand TEXT, model TEXT, barcode TEXT, uom TEXT,
    usage_type TEXT NOT NULL DEFAULT 'Administrativo' CHECK(usage_type IN ('Administrativo','Asistencial')),
    category_id INTEGER, supplier_id INTEGER, unit_cost REAL NOT NULL DEFAULT 0, min_stock REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(category_id) REFERENCES categories(id), FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
);
CREATE TABLE IF NOT EXISTS stock (product_id INTEGER, warehouse_id INTEGER, qty REAL NOT NULL DEFAULT 0, PRIMARY KEY(product_id, warehouse_id));
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL CHECK(type IN ('ENTRADA','SALIDA','TRANSFERENCIA','AJUSTE')),
    product_id INTEGER NOT NULL, from_wh INTEGER, to_wh INTEGER, qty REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0, reason TEXT,
    created_by INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, serial TEXT, type TEXT, brand TEXT, model TEXT,
    warehouse_id INTEGER, status TEXT NOT NULL DEFAULT 'Operativo' CHECK(status IN ('Operativo','Mantenimiento','Baja','Asignado')),
    assigned_to TEXT, purchase_date TEXT, cost REAL, notes TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT,
    category TEXT NOT NULL CHECK(category IN ('Incidente','Solicitud','Ajuste','Consulta')),
    priority TEXT NOT NULL CHECK(priority IN ('Baja','Media','Alta','Crítica')),
    status TEXT NOT NULL CHECK(status IN ('Abierto','En Progreso','Resuelto','Cerrado')),
    sla_hours INTEGER NOT NULL DEFAULT 48, response_sla_hours INTEGER NOT NULL DEFAULT 4,
    first_response_at TEXT, resolved_at TEXT,
    itil_type TEXT, change_risk TEXT, change_impact TEXT, planned_start TEXT, planned_end TEXT,
    approval_status TEXT, backout_plan TEXT, problem_root_cause TEXT, problem_workaround TEXT, problem_id INTEGER,
    created_by INTEGER, assigned_to INTEGER, warehouse_id INTEGER, product_id INTEGER, asset_id INTEGER,
    watchers_emails TEXT, attachment_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, due_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, author_id INTEGER, comment TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, table_name TEXT NOT NULL, record_id TEXT, user TEXT, created_at TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS password_resets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
"""
SMTP_DEFAULTS = {"smtp_server":"smtp.office365.com","smtp_port":"587","smtp_use_tls":"1","smtp_username":"","smtp_password":"","smtp_from":"","notif_on_create":"1","notif_on_resolve":"1","notif_on_status_change":"1","notif_default_to":""}

def _column_exists(table: str, column: str) -> bool:
    df = run_query(f"PRAGMA table_info({table})"); return (not df.empty) and (column in df['name'].tolist())

def migrate_schema():
    with conn() as cx: cx.executescript(INIT_SQL)
    def add_col(table, col, sqltype, default_sql=None):
        if not _column_exists(table, col):
            run_script(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}")
            if default_sql is not None:
                run_script(f"UPDATE {table} SET {col} = {default_sql} WHERE {col} IS NULL")
    for col, typ, default in [('asset_id','INTEGER', None),('watchers_emails','TEXT', "''"),('attachment_path','TEXT', "NULL"),
                              ('sla_hours','INTEGER','48'),('response_sla_hours','INTEGER','4'),('first_response_at','TEXT',"NULL"),
                              ('resolved_at','TEXT',"NULL"),('itil_type','TEXT', "'Incidente'"),('change_risk','TEXT',"NULL"),
                              ('change_impact','TEXT',"NULL"),('planned_start','TEXT',"NULL"),('planned_end','TEXT',"NULL"),
                              ('approval_status','TEXT', "'Pendiente'"),('backout_plan','TEXT',"NULL"),('problem_root_cause','TEXT',"NULL"),
                              ('problem_workaround','TEXT',"NULL"),('problem_id','INTEGER',"NULL")]:
        add_col('tickets', col, typ, default)
    for col, dtype, default_sql in [('brand','TEXT', "NULL"),('model','TEXT', "NULL"),('barcode','TEXT', "NULL"),('uom','TEXT', "NULL"),('usage_type','TEXT', "'Administrativo'")]:
        if not _column_exists('products', col):
            run_script(f"ALTER TABLE products ADD COLUMN {col} {dtype}")
            if default_sql is not None: run_script(f"UPDATE products SET {col} = {default_sql} WHERE {col} IS NULL")
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

def get_setting(key, default=None):
    df = run_query("SELECT value FROM settings WHERE key=?", (key,)); 
    return default if df.empty else df.loc[0,'value']

def set_setting(key, value):
    if run_query("SELECT 1 FROM settings WHERE key=?", (key,)).empty:
        run_script("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    else:
        run_script("UPDATE settings SET value=? WHERE key=?", (value, key))

SESSION_USER_KEY = "auth_user"
def _slug(s: str) -> str:
    import re
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "", s.replace(" ", ""))
    return s[:20] or "user"
def _generate_password(): return secrets.token_urlsafe(12)
def _find_user_by_username_or_email(identifier: str):
    df = run_query("SELECT * FROM users WHERE (username=? OR email=?) AND active=1", (identifier.strip(), identifier.strip()))
    return None if df.empty else df.iloc[0]

def _smtp_enabled(): return bool(get_setting("smtp_server")) and bool(get_setting("smtp_from"))
def send_email(to_addrs: List[str], subject: str, html_body: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    server = get_setting("smtp_server","") or ""
    port = int(get_setting("smtp_port","587") or "587")
    use_tls = get_setting("smtp_use_tls","1") == "1"
    username = get_setting("smtp_username","") or None
    password = os.getenv("APP_SMTP_PASSWORD") or (get_setting("smtp_password","") or None)
    from_addr = get_setting("smtp_from","") or username
    if not server or not from_addr: return False
    msg = MIMEMultipart(); msg['From']=from_addr; msg['To']=", ".join([a for a in to_addrs if a]); msg['Subject']=subject
    msg.attach(MIMEText(html_body,'html'))
    try:
        smtp = smtplib.SMTP(server, port, timeout=30)
        if use_tls: smtp.starttls()
        if username and password: smtp.login(username, password)
        smtp.sendmail(from_addr, to_addrs, msg.as_string()); smtp.quit(); return True
    except Exception as e:
        return False

def start_password_reset(identifier: str):
    row = _find_user_by_username_or_email(identifier)
    if row is None: return None
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow(); exp = now + timedelta(hours=1)
    run_script("INSERT INTO password_resets(user_id, token, created_at, expires_at, used) VALUES(?,?,?,?,0)", (int(row['id']), token, now.isoformat(), exp.isoformat()))
    if _smtp_enabled() and (row.get('email') or ""):
        send_email([row['email']], "Recuperar contraseña – Mesa de Ayuda", f"<p>Usuario: <b>{row['username']}</b><br/>Código: <b>{token}</b> (1h de validez)</p>")
    return token

def complete_password_reset(token: str, new_password: str) -> bool:
    df = run_query("SELECT * FROM password_resets WHERE token=? AND used=0", (token,))
    if df.empty: return False
    row = df.iloc[0]
    if datetime.utcnow() > datetime.fromisoformat(row['expires_at']): return False
    uid = int(row['user_id']); salt = secrets.token_hex(8)
    run_script("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(new_password, salt), salt, uid))
    run_script("UPDATE password_resets SET used=1 WHERE id=?", (int(row['id']),)); return True

def login_ui():
    st.title(APP_TITLE); st.caption("Usuario inicial **admin / admin**.")
    mode = st.session_state.get("mode","login")
    if mode=="signup": return signup_ui()
    if mode=="forgot": return forgot_ui()
    if mode=="reset": return reset_ui()
    with st.form("login_form"):
        user = st.text_input("Usuario"); pwd = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar")
    if submitted:
        df = run_query("SELECT * FROM users WHERE username=? AND active=1", (user.strip(),))
        if df.empty: st.error("Usuario no encontrado o inactivo."); return
        row = df.iloc[0]
        if hash_password(pwd, row["password_salt"]) == row["password_hash"]:
            st.session_state[SESSION_USER_KEY] = {"id": int(row["id"]), "username": row["username"], "role": row["role"], "email": row.get("email","")}
            st.success(f"¡Bienvenido, {row['username']}!"); st.rerun()
        else: st.error("Contraseña incorrecta.")
    c1, c2, c3 = st.columns(3)
    if c1.button("🆕 Registrarme"): st.session_state["mode"]="signup"; st.rerun()
    if c2.button("¿Olvidaste tu contraseña?"): st.session_state["mode"]="forgot"; st.rerun()
    if c3.button("Tengo un código de recuperación"): st.session_state["mode"]="reset"; st.rerun()

def forgot_ui():
    st.header("🔑 Recuperar contraseña")
    with st.form("forgot_form"):
        ident = st.text_input("Tu usuario o email *"); submitted = st.form_submit_button("Enviar código")
    if submitted:
        token = start_password_reset(ident)
        if token is None: st.error("Usuario/email no encontrado o inactivo.")
        else: st.success("Código generado (se envió por email si SMTP está configurado)."); st.code(token)
    if st.button("Ya tengo un código"): st.session_state["mode"]="reset"; st.rerun()
    if st.button("⬅️ Volver a inicio de sesión"): st.session_state["mode"]="login"; st.rerun()

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
    if st.button("⬅️ Volver a inicio de sesión"): st.session_state["mode"]="login"; st.rerun()

def require_login(): return st.session_state.get(SESSION_USER_KEY)
def log_audit(action, table_name, record_id, user, details=""):
    run_script("INSERT INTO audit_log(action, table_name, record_id, user, created_at, details) VALUES(?,?,?,?,?,?)",
               (action, table_name, str(record_id) if record_id is not None else None, user, datetime.utcnow().isoformat(), details))

def _collect_recipients(creator_email, assignee_email, watchers):
    recipients = []; 
    if creator_email: recipients.append(creator_email)
    if assignee_email: recipients.append(assignee_email)
    default_to = [e.strip() for e in (get_setting("notif_default_to","") or "").split(",") if e.strip()]
    recipients += default_to + [w for w in watchers if w]
    seen=set(); uniq=[]; 
    for r in recipients:
        if r and r not in seen: uniq.append(r); seen.add(r)
    return uniq

def _ticket_html_summary(code, title, priority, status, due_at, assignee_name):
    rows = [("Código",code),("Título",title),("Prioridad",priority),("Estado",status),("Vence",due_at or "N/A"),("Asignado a",assignee_name or "—")]
    tr = "".join([f"<tr><td style='padding:4px 8px'><b>{k}</b></td><td style='padding:4px 8px'>{v}</td></tr>" for k,v in rows])
    return f"<table border='0' cellspacing='0' cellpadding='0'>{tr}</table>"

def notify_ticket_created(code,title,creator_email,assignee_email,watchers,priority,status,due_at,assignee_name):
    if get_setting("notif_on_create","1")!="1" or not _smtp_enabled(): return
    rec = _collect_recipients(creator_email, assignee_email, watchers)
    if not rec: return
    send_email(rec, f"[Nuevo] Ticket {code}: {title}", f"<h3>Nuevo Ticket</h3>{_ticket_html_summary(code,title,priority,status,due_at,assignee_name)}")

def notify_ticket_assigned(code,title,creator_email,assignee_name,assignee_email,watchers,priority,status,due_at):
    if not _smtp_enabled(): return
    rec = _collect_recipients(creator_email, assignee_email, watchers)
    if not rec: return
    send_email(rec, f"[Asignación] Ticket {code}: {title}", f"<h3>Ticket asignado</h3><p>Asignado a <b>{assignee_name}</b>.</p>{_ticket_html_summary(code,title,priority,status,due_at,assignee_name)}")

def notify_agent_assigned(code,title,assignee_email,priority,status,due_at,assignee_name):
    if not _smtp_enabled() or not assignee_email: return
    send_email([assignee_email], f"[Asignado] Ticket {code}: {title}", f"<h3>Te asignaron un ticket</h3>{_ticket_html_summary(code,title,priority,status,due_at,assignee_name)}")

def notify_ticket_status_change(code,title,creator_email,assignee_name,assignee_email,watchers,priority,old_status,new_status,due_at):
    if get_setting("notif_on_status_change","1")!="1" or not _smtp_enabled(): return
    rec = _collect_recipients(creator_email, assignee_email, watchers)
    if not rec: return
    send_email(rec, f"[Estado: {new_status}] Ticket {code}: {title}", f"<h3>Estado actualizado</h3><p><b>{old_status}</b> → <b>{new_status}</b></p>{_ticket_html_summary(code,title,priority,new_status,due_at,assignee_name)}")

def notify_ticket_resolved(code,title,creator_email,watchers,priority,assignee_name,due_at):
    if get_setting("notif_on_resolve","1")!="1" or not _smtp_enabled(): return
    rec = _collect_recipients(creator_email, None, watchers)
    if not rec: return
    send_email(rec, f"[Resuelto] Ticket {code}: {title}", f"<h3>Ticket Resuelto</h3>{_ticket_html_summary(code,title,priority,'Resuelto',due_at,assignee_name)}")

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
        cx.execute("PRAGMA foreign_keys = ON;")
        cur = cx.execute("SELECT qty FROM stock WHERE product_id=? AND warehouse_id=?", (product_id, warehouse_id))
        row = cur.fetchone()
        if row is None: cx.execute("INSERT INTO stock(product_id,warehouse_id,qty) VALUES(?,?,?)",(product_id,warehouse_id,max(0,delta)))
        else:
            new_qty = max(0, float(row[0]) + float(delta))
            cx.execute("UPDATE stock SET qty=? WHERE product_id=? AND warehouse_id=?", (new_qty, product_id, warehouse_id))
        cx.commit()

def page_dashboard():
    st.title("📊 Dashboard – Mesa de Ayuda e Inventarios")
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
    if role == 'agente':
        now_iso = datetime.utcnow().isoformat()
        my_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso')", (user['id'],))
        my_due24 = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at BETWEEN ? AND ?", (user['id'], now_iso, (datetime.utcnow()+timedelta(hours=24)).isoformat()))
        my_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (user['id'], now_iso))
        st.subheader("👨‍🔧 Tus métricas (Agente)"); a,b,c = st.columns(3)
        a.metric("Asignados (abiertos)", int(my_open.loc[0,'n'])); b.metric("Por vencer (24h)", int(my_due24.loc[0,'n'])); c.metric("Vencidos", int(my_overdue.loc[0,'n']))
        tdf = run_query("SELECT status, COUNT(*) as n FROM tickets WHERE assigned_to=? GROUP BY status", (user['id'],))
        if not tdf.empty:
            chart = alt.Chart(tdf).mark_bar().encode(x=alt.X('status:N', title='Estado'), y=alt.Y('n:Q', title='Cantidad'), tooltip=['status','n']).properties(height=260)
            st.altair_chart(chart, use_container_width=True)
    if role == 'visor':
        now_iso = datetime.utcnow().isoformat()
        my_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE created_by=? AND status IN ('Abierto','En Progreso')", (user['id'],))
        my_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE created_by=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (user['id'], now_iso))
        st.subheader("🧑‍💼 Tus métricas"); a,b = st.columns(2)
        a.metric("Mis tickets abiertos", int(my_open.loc[0,'n'])); b.metric("Mis tickets vencidos", int(my_overdue.loc[0,'n']))
    st.subheader("Tickets – Estado y Prioridad (global)")
    tdf = run_query("SELECT status, priority, COUNT(*) as n FROM tickets GROUP BY status, priority ORDER BY status, priority")
    if not tdf.empty:
        chart = alt.Chart(tdf).mark_bar().encode(x=alt.X('status:N', title='Estado'), y=alt.Y('n:Q', title='Cantidad'), color='priority:N', tooltip=['status','priority','n']).properties(height=320)
        st.altair_chart(chart, use_container_width=True)

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
                        log_audit("UPDATE","products", int(row['id']), require_login()['username'], f"SKU={new_vals['sku']}"); st.success("Producto actualizado.")
            else:
                df = df_products()
                if df.empty: st.info("No hay productos para eliminar.")
                else:
                    sel_name = st.selectbox("Selecciona producto a eliminar", df['producto'])
                    if st.button("Eliminar definitivamente ⚠️"):
                        pid = int(df[df['producto']==sel_name]['id'].iloc[0]); run_script("DELETE FROM products WHERE id=?", (pid,)); log_audit("DELETE","products", pid, require_login()['username'], sel_name); st.success("Producto eliminado.")
    st.subheader("Listado de productos"); df = df_products()
    filtro_tipo = st.selectbox("Filtrar por tipo", ["(Todos)","Administrativo","Asistencial"], index=0)
    if filtro_tipo != "(Todos)" and not df.empty: df = df[df['tipo']==filtro_tipo]
    st.dataframe(df, use_container_width=True)

def page_tickets_nuevo():
    st.title("🎫 Nuevo Ticket"); user = require_login()
    products = run_query("SELECT id, name FROM products WHERE active=1 ORDER BY name")
    warehouses = run_query("SELECT id, name FROM warehouses ORDER BY name")
    assets = run_query("SELECT id, code || ' – ' || name AS label FROM assets WHERE active=1 ORDER BY code")
    problemas = run_query("SELECT id, code || ' – ' || title AS label FROM tickets WHERE itil_type='Problema' ORDER BY id DESC")
    with st.form("new_ticket"):
        itil_type = st.selectbox("Proceso ITIL", ["Incidente","Solicitud","Cambio","Problema"], index=0)
        title = st.text_input("Título *"); desc = st.text_area("Descripción")
        cat = st.selectbox("Categoría", ["Incidente","Solicitud","Ajuste","Consulta"], index=0)
        prio = st.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"], index=1)
        c1, c2 = st.columns(2)
        response_sla = c1.number_input("SLA 1ª respuesta (h)", min_value=1, value=4, step=1)
        resolution_sla = c2.number_input("SLA resolución (h)", min_value=1, value=48, step=1)
        cw1, cw2 = st.columns(2)
        wh_name = cw1.selectbox("Bodega relacionada", [None] + warehouses['name'].tolist())
        prod_name = cw2.selectbox("Producto relacionado", [None] + products['name'].tolist())
        asset_label = st.selectbox("Activo relacionado", [None] + assets['label'].tolist())
        change_risk = change_impact = planned_start = planned_end = backout_plan = None
        approval_status = "Pendiente"; problem_root_cause = problem_workaround = None; problem_parent_id = None
        if itil_type == "Cambio":
            st.markdown("**Datos de RFC (Cambio)**"); cc1, cc2 = st.columns(2)
            change_risk = cc1.selectbox("Riesgo", ["Bajo","Medio","Alto"]); change_impact = cc2.selectbox("Impacto", ["Bajo","Medio","Alto"])
            dp1, dp2 = st.columns(2); planned_start = dp1.date_input("Inicio planificado"); planned_end = dp2.date_input("Fin planificado")
            backout_plan = st.text_area("Plan de reversa"); approval_status = st.selectbox("Aprobación CAB", ["Pendiente","Aprobado","Rechazado"], index=0)
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
        attach_path = None
        if attachment is not None:
            tdir = os.path.join(UPLOAD_DIR, code); os.makedirs(tdir, exist_ok=True)
            fpath = os.path.join(tdir, attachment.name); open(fpath,'wb').write(attachment.read()); attach_path = fpath
        run_script("""INSERT INTO tickets(code,title,description,category,priority,status,sla_hours,response_sla_hours,created_by,assigned_to,warehouse_id,product_id,asset_id,watchers_emails,attachment_path,created_at,updated_at,due_at,itil_type,change_risk,change_impact,planned_start,planned_end,approval_status,backout_plan,problem_root_cause,problem_workaround,problem_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (code, title.strip(), desc, cat, prio, "Abierto", int(resolution_sla), int(response_sla), require_login()['id'], None, wh_id, prod_id, asset_id, watchers, attach_path, now.isoformat(), now.isoformat(), due_res.isoformat(), itil_type, change_risk, change_impact, (planned_start.isoformat() if planned_start else None), (planned_end.isoformat() if planned_end else None), approval_status, backout_plan, problem_root_cause, problem_workaround, problem_parent_id))
        creator_email = run_query("SELECT email FROM users WHERE id=?", (require_login()['id'],)).loc[0,'email']
        notify_ticket_created(code, title, creator_email, None, [e.strip() for e in (watchers or '').split(',') if e.strip()], prio, "Abierto", due_res.isoformat(), None)
        st.success(f"Ticket {code} creado correctamente.")

def page_tickets_bandeja(mis=False):
    user = require_login(); role = user['role']
    st.title("📥 Bandeja de Tickets" if not mis else "📌 Mis Tickets")
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
    if role == 'visor' and not mis: sql += " AND t.created_by=?"; params += [user['id']]
    sql += " ORDER BY t.updated_at DESC LIMIT 500"
    df = run_query(sql, tuple(params))
    if df.empty: st.info("No hay tickets que cumplan el filtro."); return
    st.dataframe(df[["code","itil_type","title","priority","status","due_at","creador","asignado","updated_at"]], use_container_width=True)
    st.divider(); st.subheader("Gestionar ticket")
    sel = st.selectbox("Selecciona ticket", df['code'].tolist()); 
    if not sel: return
    t = df[df['code']==sel].iloc[0]
    st.markdown(f"**{t['title']}**"); st.caption(f"ITIL: {t.get('itil_type','Incidente')} · Estado: {t['status']} · Prioridad: {t['priority']} · SLA resp: {t.get('response_sla_hours',4)}h · SLA res: {t['sla_hours']}h · Vence: {t['due_at']}")
    st.write(t['description'] or "(Sin descripción)")
    # permisos
    is_admin = (role == 'admin'); is_agent = (role == 'agente')
    assigned_to_me = (pd.notna(t['assigned_to']) and int(t['assigned_to']) == user['id'])
    unassigned = pd.isna(t['assigned_to']) or (str(t['assigned_to']).strip() == "")
    can_edit_status = is_admin or (is_agent and assigned_to_me); can_adjust_sla = is_admin or (is_agent and assigned_to_me)
    can_update_watchers = can_edit_status; can_assign_admin = is_admin; can_self_assign = is_agent and (unassigned or assigned_to_me)
    can_add_comment = is_admin or is_agent or (pd.notna(t['created_by']) and int(t['created_by']) == user['id'])
    users_df = run_query("SELECT id, username, email FROM users WHERE active=1 ORDER BY username")
    colA, colB, colC = st.columns(3)
    if can_edit_status: new_status = colA.selectbox("Cambiar estado", ["Abierto","En Progreso","Resuelto","Cerrado"], index=["Abierto","En Progreso","Resuelto","Cerrado"].index(t['status']))
    else: colA.text_input("Estado", value=t['status'], disabled=True); new_status = t['status']
    assignee = t['asignado'] if pd.notna(t['asignado']) else None
    assignee_email = t.get('asignado_email', None) if pd.notna(t.get('asignado_email', None)) else None
    if can_assign_admin:
        assignee = colB.selectbox("Asignar a", [None] + users_df['username'].tolist(), index=(users_df['username'].tolist().index(t['asignado'])+1 if pd.notna(t['asignado']) and t['asignado'] in users_df['username'].tolist() else 0))
        assignee_email = str(users_df.loc[users_df['username']==assignee,'email'].iloc[0]) if assignee else None
    elif can_self_assign:
        options = [None, user['username']]; default_index = options.index(t['asignado']) if pd.notna(t['asignado']) and t['asignado'] in options else 0
        assignee = colB.selectbox("Asignar a (solo tú)", options, index=default_index); assignee_email = user['email'] if assignee == user['username'] else None
    else: colB.text_input("Asignado a", value=(t['asignado'] or ""), disabled=True)
    more_hours = colC.number_input("Ajustar SLA resolución (+h)", min_value=0, value=0, disabled=not can_adjust_sla)
    watchers = st.text_input("Watchers (coma)", value=t.get('watchers_emails','') or '', disabled=not can_update_watchers)
    if can_edit_status and not t.get('first_response_at'):
        if st.button("✅ Marcar 1ª respuesta ahora"):
            now = datetime.utcnow().isoformat(); run_script("UPDATE tickets SET first_response_at=?, updated_at=? WHERE id=?", (now, now, int(t['id']))); st.success("Primera respuesta registrada."); st.rerun()
    else:
        if t.get('first_response_at'): st.info(f"1ª respuesta: {t['first_response_at']}")
    itil_type = t.get('itil_type','Incidente')
    risk=impact=ps=pe=approval=backout=root_cause=workaround=None
    if itil_type == "Cambio":
        st.markdown("**RFC (Cambio)**"); c1, c2 = st.columns(2)
        risk = c1.selectbox("Riesgo", ["Bajo","Medio","Alto"], index=(["Bajo","Medio","Alto"].index(t.get('change_risk','Bajo')) if t.get('change_risk') in ["Bajo","Medio","Alto"] else 0), disabled=not can_edit_status)
        impact = c2.selectbox("Impacto", ["Bajo","Medio","Alto"], index=(["Bajo","Medio","Alto"].index(t.get('change_impact','Bajo')) if t.get('change_impact') in ["Bajo","Medio","Alto"] else 0), disabled=not can_edit_status)
        d1, d2 = st.columns(2)
        ps = d1.text_input("Inicio planificado (YYYY-MM-DD)", value=(t.get('planned_start') or ""), disabled=not can_edit_status)
        pe = d2.text_input("Fin planificado (YYYY-MM-DD)", value=(t.get('planned_end') or ""), disabled=not can_edit_status)
        approval = st.selectbox("Aprobación CAB", ["Pendiente","Aprobado","Rechazado"], index=(["Pendiente","Aprobado","Rechazado"].index(t.get('approval_status','Pendiente'))), disabled=not can_edit_status)
        backout = st.text_area("Plan de reversa", value=t.get('backout_plan') or "", disabled=not can_edit_status)
    if itil_type == "Problema":
        st.markdown("**Análisis de Problema**"); pc1, pc2 = st.columns(2)
        root_cause = pc1.text_input("Causa raíz", value=t.get('problem_root_cause') or "", disabled=not can_edit_status)
        workaround = pc2.text_input("Workaround", value=t.get('problem_workaround') or "", disabled=not can_edit_status)
    comment = st.text_area("Agregar comentario", disabled=not can_add_comment)
    if (can_edit_status or can_assign_admin or can_self_assign or can_adjust_sla) and st.button("Guardar cambios"):
        now = datetime.utcnow(); assignee_id=None; assignee_email_eff=None; assignee_name_eff=None
        if assignee:
            if assignee == user['username']: assignee_id=user['id']; assignee_email_eff=user['email']; assignee_name_eff=assignee
            elif assignee in users_df['username'].tolist():
                assignee_id=int(users_df.loc[users_df['username']==assignee,'id'].iloc[0]); assignee_email_eff=str(users_df.loc[users_df['username']==assignee,'email'].iloc[0]); assignee_name_eff=assignee
        due_at = t['due_at']
        if more_hours > 0 and can_adjust_sla:
            try: due_at = (datetime.fromisoformat(due_at) + timedelta(hours=int(more_hours))).isoformat() if due_at else (now + timedelta(hours=int(more_hours))).isoformat()
            except Exception: due_at = (now + timedelta(hours=int(more_hours))).isoformat()
        closed_at = t['closed_at']; resolved_at = t.get('resolved_at')
        if new_status in ("Resuelto","Cerrado") and can_edit_status:
            resolved_at = now.isoformat()
            if new_status == "Cerrado": closed_at = now.isoformat()
        run_script("""UPDATE tickets SET status=?, assigned_to=?, updated_at=?, due_at=?, closed_at=?, watchers_emails=?,
                                       change_risk=?, change_impact=?, planned_start=?, planned_end=?, approval_status=?, backout_plan=?,
                                       problem_root_cause=?, problem_workaround=?, resolved_at=? WHERE id=?""",
                   (new_status, assignee_id, now.isoformat(), due_at, closed_at, watchers if can_update_watchers else t.get('watchers_emails',''), risk, impact, ps, pe, approval, backout, root_cause, workaround, resolved_at, int(t['id'])))
        if comment and can_add_comment:
            run_script("INSERT INTO ticket_comments(ticket_id,author_id,comment,created_at) VALUES(?,?,?,?)", (int(t['id']), user['id'], comment, now.isoformat()))
        creator_email = t.get('creador_email',''); watchers_list = [e.strip() for e in (watchers or '').split(',') if e.strip()]
        if assignee:
            notify_ticket_assigned(t['code'], t['title'], creator_email, assignee, assignee_email_eff, watchers_list, t['priority'], new_status, due_at)
            notify_agent_assigned(t['code'], t['title'], assignee_email_eff, t['priority'], new_status, due_at, assignee)
        if new_status != t['status']:
            notify_ticket_status_change(t['code'], t['title'], creator_email, assignee or t.get('asignado'), assignee_email_eff or t.get('asignado_email'), watchers_list, t['priority'], t['status'], new_status, due_at)
        if new_status == "Resuelto" and can_edit_status:
            notify_ticket_resolved(t['code'], t['title'], creator_email, watchers_list, t['priority'], assignee or t.get('asignado'), due_at)
        st.success("Ticket actualizado."); st.rerun()
    st.subheader("Comentarios")
    cdf = run_query("SELECT c.created_at, u.username AS autor, c.comment FROM ticket_comments c LEFT JOIN users u ON u.id=c.author_id WHERE c.ticket_id=? ORDER BY c.id DESC", (int(t['id']),))
    st.dataframe(cdf, use_container_width=True)

def page_reportes_export():
    user = require_login()
    if user['role'] != 'admin': st.error("Solo administradores."); return
    st.title("📤 Reportes / Exportar")
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
        c1,c2,c3 = st.columns(3); c1.metric("Cumplimiento 1ª respuesta", f"{first_ratio:,.1f}%"); c2.metric("Cumplimiento resolución", f"{res_ratio:,.1f}%"); c3.metric("MTTR (horas)", f"{mttr_h:,.1f}")
    import io; excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine='xlsxwriter') as w: inv.to_excel(w, sheet_name='inventario', index=False); t.to_excel(w, sheet_name='tickets', index=False)
    st.download_button("⬇️ XLSX Inventario+Tickets", excel_buf.getvalue(), file_name="reportes_inventario_tickets.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def page_config_usuarios():
    st.title("⚙️ Configuración – Usuarios"); user = require_login()
    if user['role'] != 'admin': st.error("Solo administradores."); return
    st.subheader("Crear usuario")
    with st.form("new_user"):
        uname = st.text_input("Usuario *"); email = st.text_input("Email"); role = st.selectbox("Rol", ["admin","agente","visor"], index=1)
        pwd1 = st.text_input("Contraseña *", type="password"); pwd2 = st.text_input("Confirmar contraseña *", type="password")
        submitted = st.form_submit_button("Crear")
    if submitted:
        if not uname or not pwd1 or not pwd2: st.error("Campos obligatorios.")
        elif pwd1 != pwd2: st.error("Las contraseñas no coinciden.")
        else:
            salt = secrets.token_hex(8)
            try:
                run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)", (uname.strip(), email.strip(), hash_password(pwd1, salt), salt, role, datetime.utcnow().isoformat())); st.success("Usuario creado.")
            except sqlite3.IntegrityError: st.error("Usuario ya existe.")
    st.subheader("Usuarios existentes"); df = run_query("SELECT id, username, email, role, active, created_at FROM users ORDER BY username"); st.dataframe(df, use_container_width=True)
    st.subheader("Cambiar mi contraseña")
    with st.form("pwd_form"):
        oldp = st.text_input("Contraseña actual", type="password"); newp = st.text_input("Nueva contraseña", type="password"); submitted2 = st.form_submit_button("Actualizar contraseña")
    if submitted2:
        me = run_query("SELECT * FROM users WHERE id=?", (user['id'],)).iloc[0]
        if hash_password(oldp, me['password_salt']) != me['password_hash']: st.error("Contraseña actual incorrecta.")
        else:
            salt = secrets.token_hex(8); run_script("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(newp, salt), salt, user['id'])); st.success("Contraseña actualizada.")
    st.subheader("Resetear contraseña de un usuario (admin)")
    df2 = run_query("SELECT id, username, email FROM users ORDER BY username")
    if not df2.empty:
        ureset = st.selectbox("Usuario a resetear", df2['username'].tolist())
        if st.button("Generar contraseña temporal"):
            temp = secrets.token_urlsafe(10); salt = secrets.token_hex(8); uid = int(df2.loc[df2['username']==ureset, 'id'].iloc[0])
            run_script("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(temp, salt), salt, uid)); st.success(f"Contraseña temporal para **{ureset}**: `{temp}`")
            email_to = str(df2.loc[df2['username']==ureset, 'email'].iloc[0] or "")
            if email_to and _smtp_enabled(): send_email([email_to], "Reset de contraseña – Mesa de Ayuda", f"<p>Tu nueva contraseña temporal: <b>{temp}</b>.</p>")

def page_config_notificaciones():
    st.title("📧 Configuración – Notificaciones (SMTP)")
    if require_login()['role'] != 'admin': st.error("Solo administradores."); return
    st.info("La contraseña SMTP es mejor definirla en el entorno **APP_SMTP_PASSWORD**.")
    with st.form("smtp_form"):
        server = st.text_input("Servidor SMTP", value=get_setting("smtp_server","")); port = st.number_input("Puerto", min_value=1, value=int(get_setting("smtp_port","587") or 587))
        use_tls = st.checkbox("Usar TLS", value=get_setting("smtp_use_tls","1")=="1"); username = st.text_input("Usuario SMTP", value=get_setting("smtp_username",""))
        password = st.text_input("Contraseña SMTP (opcional si usas APP_SMTP_PASSWORD)", type="password", value=get_setting("smtp_password","")); from_addr = st.text_input("Remitente (From)", value=get_setting("smtp_from", username or ""))
        notif_create = st.checkbox("Notificar en creación de ticket", value=get_setting("notif_on_create","1")=="1")
        notif_resolve = st.checkbox("Notificar en resolución de ticket", value=get_setting("notif_on_resolve","1")=="1")
        notif_change = st.checkbox("Notificar en cada cambio de estado", value=get_setting("notif_on_status_change","1")=="1")
        default_to = st.text_input("Correos por defecto (coma)", value=get_setting("notif_default_to","")); submitted = st.form_submit_button("Guardar configuración")
    if submitted:
        set_setting("smtp_server", server.strip()); set_setting("smtp_port", str(int(port))); set_setting("smtp_use_tls", "1" if use_tls else "0")
        set_setting("smtp_username", username.strip()); 
        if password: set_setting("smtp_password", password)
        set_setting("smtp_from", from_addr.strip()); set_setting("notif_on_create", "1" if notif_create else "0")
        set_setting("notif_on_resolve", "1" if notif_resolve else "0"); set_setting("notif_on_status_change", "1" if notif_change else "0"); set_setting("notif_default_to", default_to.strip())
        st.success("Configuración guardada.")
    st.subheader("Probar envío"); to = st.text_input("Enviar correo de prueba a:")
    if st.button("Enviar prueba"):
        ok = send_email([to], "Prueba SMTP – Mesa de Ayuda", "<p>Mensaje de prueba correcto.</p>"); st.success("Correo enviado.") if ok else st.error("Error enviando correo.")

def _username_available(u: str) -> bool: return run_query("SELECT 1 FROM users WHERE username=?", (u,)).empty
def signup_ui():
    st.header("🆕 Crear cuenta"); st.caption("Se creará una cuenta con rol **visor**. Un admin puede elevar a agente o admin.")
    with st.form("signup_form"):
        full_name = st.text_input("Nombre completo"); email = st.text_input("Email *"); submitted = st.form_submit_button("Crear mi cuenta")
    if submitted:
        if not email or "@" not in email: st.error("Email inválido."); return
        base = (email.split('@')[0] or _slug(full_name)); username = _slug(base)
        if not _username_available(username):
            suffix = 1
            while not _username_available(f"{username}{suffix}") and suffix < 9999:
                suffix += 1
            username = f"{username}{suffix}"
        pwd_plain = secrets.token_urlsafe(12); salt = secrets.token_hex(8)
        run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                   (username, email.strip(), hash_password(pwd_plain, salt), salt, "visor", datetime.utcnow().isoformat()))
        st.success("✅ Cuenta creada."); st.info(f"**Tu usuario:** `{username}`  \n**Tu contraseña:** `{pwd_plain}`")
        if _smtp_enabled(): send_email([email.strip()], "Credenciales de acceso – Mesa de Ayuda", f"<p>Usuario: <b>{username}</b><br/>Contraseña: <b>{pwd_plain}</b></p>")
    if st.button("⬅️ Volver a Iniciar sesión"): st.session_state["mode"]="login"; st.rerun()

def sidebar_menu():
    user = require_login(); st.sidebar.title("Navegación"); st.sidebar.write(f"👤 **{user['username']}** · Rol: *{user['role']}*")
    role = user['role']
    if role == 'admin':
        items = ["Dashboard","Inventario – Productos","Tickets – Nuevo","Tickets – Bandeja","Tickets – Mis Tickets","Reportes / Exportar","Configuración – Usuarios","Configuración – Notificaciones"]
    elif role == 'agente':
        items = ["Dashboard","Tickets – Nuevo","Tickets – Bandeja","Tickets – Mis Tickets"]
    else:
        items = ["Dashboard","Tickets – Nuevo","Tickets – Mis Tickets"]
    menu = st.sidebar.radio("Ir a", items, index=0)
    if st.sidebar.button("Cerrar sesión"): st.session_state.pop(SESSION_USER_KEY, None); st.rerun()
    return menu

def router():
    user = require_login()
    if not user: login_ui(); return
    page = sidebar_menu()
    if page == "Dashboard": page_dashboard()
    elif page == "Inventario – Productos": page_inventario_productos()
    elif page == "Tickets – Nuevo": page_tickets_nuevo()
    elif page == "Tickets – Bandeja": page_tickets_bandeja(mis=False)
    elif page == "Tickets – Mis Tickets": page_tickets_bandeja(mis=True)
    elif page == "Reportes / Exportar": page_reportes_export()
    elif page == "Configuración – Usuarios": page_config_usuarios()
    elif page == "Configuración – Notificaciones": page_config_notificaciones()

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="📦", layout="wide")
    init_db(); router()

if __name__ == "__main__": main()
