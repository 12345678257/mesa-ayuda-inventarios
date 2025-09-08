# app_mesa_ayuda_inventarios_streamlit.py
# --------------------------------------------------------------
# Mesa de Ayuda (Help Desk) + Inventarios – Streamlit (v2.4 PRO)
# Autor: ChatGPT (GPT-5 Thinking) – 2025-09-06
# --------------------------------------------------------------
# Novedades v2.4 PRO:
# - Interfaz por rol (admin/agente/visor) con permisos diferenciados.
# - Agente: métricas personales en el Dashboard (asignados, por vencer 24h, vencidos).
# - Visor: solo ve sus propios tickets; puede comentar, no editar.
# - Edición restringida:
#     * Admin: total.
#     * Agente: solo tickets asignados a él (y puede autoasignarse si está sin asignar).
#     * Visor: sin edición (solo comentarios en sus tickets).
# - Notificación al dueño y al agente en asignación (ya incluido).
# - Migraciones automáticas y robustez ante esquemas antiguos.
# --------------------------------------------------------------

import os
import io
import re
import secrets
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List

import pandas as pd
import streamlit as st
import altair as alt

APP_TITLE = "Mesa de Ayuda e Inventarios"
DB_PATH = os.path.join(os.path.dirname(__file__), "inventarios_helpdesk.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

# ----------------------------- Utilidades -----------------------------

def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _retry_migrating(fn, *args, **kwargs):
    """Ejecuta fn(); si hay error de columna faltante, corre migraciones y reintenta 1 vez."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        if "no such column" in msg or "has no column" in msg:
            migrate_schema()
            return fn(*args, **kwargs)
        raise


def run_script(sql: str, params: tuple = ()):  # INSERT/UPDATE/DDL
    def _exec():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            cx.execute(sql, params)
            cx.commit()
    return _retry_migrating(_exec)


def run_many(sql: str, rows: List[tuple]):
    def _execmany():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            cx.executemany(sql, rows)
            cx.commit()
    return _retry_migrating(_execmany)


def run_query(sql: str, params: tuple = ()):  # SELECT → DataFrame
    def _q():
        with conn() as cx:
            cx.execute("PRAGMA foreign_keys = ON;")
            return pd.read_sql_query(sql, cx, params=params)
    return _retry_migrating(_q)


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


# ----------------------------- DB Init & Migraciones -----------------------------

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

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    location TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    contact TEXT,
    email TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    barcode TEXT,
    uom TEXT,
    usage_type TEXT NOT NULL DEFAULT 'Administrativo' CHECK(usage_type IN ('Administrativo','Asistencial')),
    category_id INTEGER,
    supplier_id INTEGER,
    unit_cost REAL NOT NULL DEFAULT 0,
    min_stock REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(category_id) REFERENCES categories(id),
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS stock (
    product_id INTEGER,
    warehouse_id INTEGER,
    qty REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(product_id, warehouse_id),
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY(warehouse_id) REFERENCES warehouses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('ENTRADA','SALIDA','TRANSFERENCIA','AJUSTE')),
    product_id INTEGER NOT NULL,
    from_wh INTEGER,
    to_wh INTEGER,
    qty REAL NOT NULL,
    unit_cost REAL NOT NULL DEFAULT 0,
    reason TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY(from_wh) REFERENCES warehouses(id),
    FOREIGN KEY(to_wh) REFERENCES warehouses(id),
    FOREIGN KEY(created_by) REFERENCES users(id)
);

-- Activos fijos
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    serial TEXT,
    type TEXT,
    brand TEXT,
    model TEXT,
    warehouse_id INTEGER,
    status TEXT NOT NULL DEFAULT 'Operativo' CHECK(status IN ('Operativo','Mantenimiento','Baja','Asignado')),
    assigned_to TEXT,
    purchase_date TEXT,
    cost REAL,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(warehouse_id) REFERENCES warehouses(id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT NOT NULL CHECK(category IN ('Incidente','Solicitud','Ajuste','Consulta')),
    priority TEXT NOT NULL CHECK(priority IN ('Baja','Media','Alta','Crítica')),
    status TEXT NOT NULL CHECK(status IN ('Abierto','En Progreso','Resuelto','Cerrado')),
    sla_hours INTEGER NOT NULL DEFAULT 48,
    created_by INTEGER,
    assigned_to INTEGER,
    warehouse_id INTEGER,
    product_id INTEGER,
    asset_id INTEGER,
    watchers_emails TEXT,
    attachment_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    due_at TEXT,
    closed_at TEXT,
    FOREIGN KEY(created_by) REFERENCES users(id),
    FOREIGN KEY(assigned_to) REFERENCES users(id),
    FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
    FOREIGN KEY(product_id) REFERENCES products(id),
    FOREIGN KEY(asset_id) REFERENCES assets(id)
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    author_id INTEGER,
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
    FOREIGN KEY(author_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id TEXT,
    user TEXT,
    created_at TEXT NOT NULL,
    details TEXT
);
"""

SMTP_DEFAULTS = {
    "smtp_server": "smtp.office365.com",
    "smtp_port": "587",
    "smtp_use_tls": "1",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from": "",
    "notif_on_create": "1",
    "notif_on_resolve": "1",
    "notif_default_to": ""
}

def _column_exists(table: str, column: str) -> bool:
    df = run_query(f"PRAGMA table_info({table})")
    return (not df.empty) and (column in df['name'].tolist())


def migrate_schema():
    """Aplica alteraciones no destructivas para esquemas antiguos."""
    # tickets: asset_id, watchers_emails, attachment_path, sla_hours
    if not _column_exists('tickets', 'asset_id'):
        run_script("ALTER TABLE tickets ADD COLUMN asset_id INTEGER")
    if not _column_exists('tickets', 'watchers_emails'):
        run_script("ALTER TABLE tickets ADD COLUMN watchers_emails TEXT")
    if not _column_exists('tickets', 'attachment_path'):
        run_script("ALTER TABLE tickets ADD COLUMN attachment_path TEXT")
    if not _column_exists('tickets', 'sla_hours'):
        run_script("ALTER TABLE tickets ADD COLUMN sla_hours INTEGER NOT NULL DEFAULT 48")

    # products: brand, model, barcode, uom, usage_type
    for col, dtype in [('brand','TEXT'),('model','TEXT'),('barcode','TEXT'),('uom','TEXT'),('usage_type','TEXT')]:
        if not _column_exists('products', col):
            default_clause = " DEFAULT 'Administrativo'" if col == 'usage_type' else ""
            run_script(f"ALTER TABLE products ADD COLUMN {col} {dtype}{default_clause}")
            if col == 'usage_type':
                run_script("UPDATE products SET usage_type='Administrativo' WHERE usage_type IS NULL OR usage_type='' ")

    # settings: defaults
    for k, v in SMTP_DEFAULTS.items():
        if run_query("SELECT 1 FROM settings WHERE key=?", (k,)).empty:
            run_script("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))


def init_db():
    ensure_dirs()
    with conn() as cx:
        cx.executescript(INIT_SQL)
    migrate_schema()  # <- importante para DB existentes

    # bootstrap admin/seed
    df = run_query("SELECT COUNT(*) AS n FROM users")
    if int(df.loc[0, 'n']) == 0:
        salt = secrets.token_hex(8)
        pw = "admin"
        run_script(
            "INSERT INTO users (username,email,password_hash,password_salt,role,active,created_at) VALUES (?,?,?,?,?,?,?)",
            ("admin","admin@example.com",hash_password(pw, salt),salt,"admin",1,datetime.utcnow().isoformat())
        )
        # datos mínimos
        run_script("INSERT OR IGNORE INTO warehouses(name,location) VALUES(?,?)", ("Bodega Central","Bogotá"))
        run_script("INSERT OR IGNORE INTO categories(name) VALUES(?)", ("General",))
        run_script("INSERT OR IGNORE INTO suppliers(name,contact,email,phone) VALUES(?,?,?,?)", ("Proveedor Demo","Contacto","proveedor@demo.com","3000000000"))
        run_script(
            "INSERT OR IGNORE INTO products(sku,name,brand,model,barcode,uom,usage_type,category_id,supplier_id,unit_cost,min_stock,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            ("SKU-001","Producto Demo","DemoBrand","X1","000111222333","UND","Administrativo",1,1,1000,10)
        )
        with conn() as cx:
            cx.execute("INSERT OR IGNORE INTO stock(product_id,warehouse_id,qty) VALUES(1,1,0)")
            cx.commit()


# ----------------------------- Settings helpers -----------------------------

def get_setting(key:str, default:Optional[str]=None) -> Optional[str]:
    df = run_query("SELECT value FROM settings WHERE key=?", (key,))
    if df.empty:
        return default
    return df.loc[0, 'value']


def set_setting(key:str, value:str):
    if run_query("SELECT 1 FROM settings WHERE key=?", (key,)).empty:
        run_script("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    else:
        run_script("UPDATE settings SET value=? WHERE key=?", (value, key))


# ----------------------------- Seguridad & Sesión -----------------------------

SESSION_USER_KEY = "auth_user"

def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "", s.replace(" ", ""))
    return s[:20] or "user"


def _generate_password() -> str:
    # 12-16 caracteres, seguro
    return secrets.token_urlsafe(12)


def login_ui():
    st.title(APP_TITLE)
    st.caption("Usuario inicial **admin** / **admin**. Cámbialo en Configuración → Usuarios.")

    if st.session_state.get("mode") == "signup":
        return signup_ui()

    with st.form("login_form"):
        user = st.text_input("Usuario")
        pwd = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar")
    if submitted:
        df = run_query("SELECT * FROM users WHERE username=? AND active=1", (user.strip(),))
        if df.empty:
            st.error("Usuario no encontrado o inactivo.")
            return
        row = df.iloc[0]
        if hash_password(pwd, row["password_salt"]) == row["password_hash"]:
            st.session_state[SESSION_USER_KEY] = {
                "id": int(row["id"]),
                "username": row["username"],
                "role": row["role"],
                "email": row.get("email", "")
            }
            st.success(f"¡Bienvenido, {row['username']}!")
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")

    st.markdown("¿No tienes cuenta?")
    if st.button("🆕 Registrarme"):
        st.session_state["mode"] = "signup"
        st.rerun()


def require_login():
    return st.session_state.get(SESSION_USER_KEY)


# ----------------------------- Auditoría -----------------------------

def log_audit(action, table_name, record_id, user, details=""):
    run_script(
        "INSERT INTO audit_log(action, table_name, record_id, user, created_at, details) VALUES(?,?,?,?,?,?)",
        (action, table_name, str(record_id) if record_id is not None else None, user, datetime.utcnow().isoformat(), details)
    )


# ----------------------------- E-mail -----------------------------

def _smtp_enabled():
    return get_setting("smtp_server") and get_setting("smtp_from")


def send_email(to_addrs: List[str], subject: str, html_body: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    server = get_setting("smtp_server", "") or ""
    port = int(get_setting("smtp_port", "587") or "587")
    use_tls = get_setting("smtp_use_tls", "1") == "1"
    username = get_setting("smtp_username", "") or None
    password = os.getenv("APP_SMTP_PASSWORD") or (get_setting("smtp_password", "") or None)
    from_addr = get_setting("smtp_from", "") or username

    if not server or not from_addr:
        return False

    msg = MIMEMultipart()
    msg['From'] = from_addr
    msg['To'] = ", ".join(to_addrs)
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))

    try:
        smtp = smtplib.SMTP(server, port, timeout=30)
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.sendmail(from_addr, to_addrs, msg.as_string())
        smtp.quit()
        return True
    except Exception as e:
        st.warning(f"No se pudo enviar correo: {e}")
        return False


def notify_ticket_created(code:str, title:str, creator_email:str, assignee_email:Optional[str], watchers:List[str]):
    if get_setting("notif_on_create","1") != "1" or not _smtp_enabled():
        return
    recipients = []
    if creator_email:
        recipients.append(creator_email)
    if assignee_email:
        recipients.append(assignee_email)
    default_to = [e.strip() for e in (get_setting("notif_default_to","" ) or "").split(',') if e.strip()]
    recipients += default_to
    recipients += [w for w in watchers if w]
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    body = f"""
    <h3>Nuevo Ticket: {code}</h3>
    <p><b>Título:</b> {title}</p>
    <p>Se ha creado un nuevo ticket en la Mesa de Ayuda.</p>
    """
    send_email(recipients, f"Nuevo ticket {code}", body)


def notify_ticket_resolved(code:str, title:str, creator_email:str, watchers:List[str]):
    if get_setting("notif_on_resolve","1") != "1" or not _smtp_enabled():
        return
    recipients = []
    if creator_email:
        recipients.append(creator_email)
    default_to = [e.strip() for e in (get_setting("notif_default_to","" ) or "").split(',') if e.strip()]
    recipients += default_to
    recipients += [w for w in watchers if w]
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    body = f"""
    <h3>Ticket Resuelto: {code}</h3>
    <p><b>Título:</b> {title}</p>
    <p>El ticket ha sido marcado como <b>Resuelto</b>.</p>
    """
    send_email(recipients, f"Ticket resuelto {code}", body)


def notify_ticket_assigned(code: str, title: str, creator_email: str, assignee_name: str, assignee_email: Optional[str], watchers: List[str]):
    """Notifica al dueño (y watchers) cuando se asigna un agente."""
    if not _smtp_enabled():
        return
    recipients = []
    if creator_email:
        recipients.append(creator_email)
    default_to = [e.strip() for e in (get_setting("notif_default_to","") or "").split(",") if e.strip()]
    recipients += default_to
    recipients += [w for w in watchers if w]
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    assignee_text = f"{assignee_name} ({assignee_email})" if assignee_email else assignee_name
    body = f"""
    <h3>Ticket asignado: {code}</h3>
    <p><b>Título:</b> {title}</p>
    <p>Este ticket ha sido asignado a <b>{assignee_text}</b>.</p>
    """
    send_email(recipients, f"Ticket {code} asignado a {assignee_name}", body)


def notify_agent_assigned(code: str, title: str, assignee_email: Optional[str]):
    """Notifica directamente al agente asignado."""
    if not _smtp_enabled() or not assignee_email:
        return
    body = f"""
    <h3>Te asignaron un ticket: {code}</h3>
    <p><b>Título:</b> {title}</p>
    <p>Por favor revisa la Mesa de Ayuda.</p>
    """
    send_email([assignee_email], f"Nuevo ticket asignado: {code}", body)


# ----------------------------- Helpers de datos -----------------------------

def df_products():
    return run_query(
        """
        SELECT p.id, p.sku, p.name AS producto, p.brand AS marca, p.model AS modelo, p.barcode AS codigo_barras,
               p.uom AS unidad, p.usage_type AS tipo, c.name AS categoria, s.name AS proveedor,
               p.unit_cost AS costo_unit, p.min_stock AS stock_min, p.active
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        ORDER BY p.name
        """
    )


def df_stock():
    return run_query(
        """
        SELECT s.product_id, s.warehouse_id, w.name AS bodega, p.name AS producto, p.usage_type AS tipo, s.qty, p.unit_cost,
               (s.qty * p.unit_cost) AS valor
        FROM stock s
        JOIN products p ON p.id = s.product_id
        JOIN warehouses w ON w.id = s.warehouse_id
        ORDER BY p.name, w.name
        """
    )


def adjust_stock(product_id:int, warehouse_id:int, delta:float):
    with conn() as cx:
        cx.execute("PRAGMA foreign_keys = ON;")
        cur = cx.execute("SELECT qty FROM stock WHERE product_id=? AND warehouse_id=?", (product_id, warehouse_id))
        row = cur.fetchone()
        if row is None:
            cx.execute("INSERT INTO stock(product_id, warehouse_id, qty) VALUES(?,?,?)", (product_id, warehouse_id, max(0, delta)))
        else:
            new_qty = max(0, float(row[0]) + float(delta))
            cx.execute("UPDATE stock SET qty=? WHERE product_id=? AND warehouse_id=?", (new_qty, product_id, warehouse_id))
        cx.commit()


# ----------------------------- Dashboard -----------------------------

def page_dashboard():
    st.title("📊 Dashboard – Mesa de Ayuda e Inventarios")
    user = require_login()
    role = user['role']

    stock_df = df_stock()
    total_valor = float(stock_df["valor"].sum()) if not stock_df.empty else 0.0

    low_df = run_query(
        """
        SELECT p.id, p.name AS producto, COALESCE(SUM(s.qty),0) AS stock_total, p.min_stock
        FROM products p
        LEFT JOIN stock s ON s.product_id = p.id
        GROUP BY p.id
        HAVING stock_total < p.min_stock
        ORDER BY stock_total ASC
        """
    )

    tickets_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE status IN ('Abierto','En Progreso')")
    tickets_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (datetime.utcnow().isoformat(),))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valor inventario", f"${total_valor:,.0f}")
    c2.metric("Productos bajo stock", int(low_df.shape[0]))
    c3.metric("Tickets abiertos", int(tickets_open.loc[0, 'n']))
    c4.metric("Tickets vencidos", int(tickets_overdue.loc[0, 'n']))

    # Métricas específicas por rol
    if role == 'agente':
        now_iso = datetime.utcnow().isoformat()
        my_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso')", (user['id'],))
        my_due24 = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at BETWEEN ? AND ?", (user['id'], now_iso, (datetime.utcnow()+timedelta(hours=24)).isoformat()))
        my_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE assigned_to=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (user['id'], now_iso))
        st.subheader("👨‍🔧 Tus métricas (Agente)")
        a,b,c = st.columns(3)
        a.metric("Asignados (abiertos)", int(my_open.loc[0,'n']))
        b.metric("Por vencer (24h)", int(my_due24.loc[0,'n']))
        c.metric("Vencidos", int(my_overdue.loc[0,'n']))
        # Gráfico por estado para asignados a mí
        tdf = run_query("SELECT status, COUNT(*) as n FROM tickets WHERE assigned_to=? GROUP BY status", (user['id'],))
        if not tdf.empty:
            chart = alt.Chart(tdf).mark_bar().encode(
                x=alt.X('status:N', title='Estado'),
                y=alt.Y('n:Q', title='Cantidad'),
                tooltip=['status','n']
            ).properties(height=260)
            st.altair_chart(chart, use_container_width=True)

    if role == 'visor':
        now_iso = datetime.utcnow().isoformat()
        my_open = run_query("SELECT COUNT(*) AS n FROM tickets WHERE created_by=? AND status IN ('Abierto','En Progreso')", (user['id'],))
        my_overdue = run_query("SELECT COUNT(*) AS n FROM tickets WHERE created_by=? AND status IN ('Abierto','En Progreso') AND due_at IS NOT NULL AND due_at < ?", (user['id'], now_iso))
        st.subheader("🧑‍💼 Tus métricas")
        a,b = st.columns(2)
        a.metric("Mis tickets abiertos", int(my_open.loc[0,'n']))
        b.metric("Mis tickets vencidos", int(my_overdue.loc[0,'n']))

    st.subheader("Tickets – Estado y Prioridad (global)")
    tdf = run_query("SELECT status, priority, COUNT(*) as n FROM tickets GROUP BY status, priority ORDER BY status, priority")
    if tdf.empty:
        st.info("No hay tickets aún.")
    else:
        chart = alt.Chart(tdf).mark_bar().encode(
            x=alt.X('status:N', title='Estado'),
            y=alt.Y('n:Q', title='Cantidad'),
            color='priority:N',
            tooltip=['status','priority','n']
        ).properties(height=320)
        st.altair_chart(chart, use_container_width=True)

    st.subheader("Inventario por bodega (Top 20 por valor)")
    if stock_df.empty:
        st.info("Sin datos de inventario.")
    else:
        top = stock_df.groupby(['bodega'])['valor'].sum().reset_index().sort_values('valor', ascending=False).head(20)
        chart2 = alt.Chart(top).mark_bar().encode(
            x=alt.X('bodega:N', sort='-y', title='Bodega'),
            y=alt.Y('valor:Q', title='Valor'),
            tooltip=['bodega','valor']
        ).properties(height=320)
        st.altair_chart(chart2, use_container_width=True)

    st.subheader("Alertas de bajo stock")
    if low_df.empty:
        st.success("✅ No hay productos por debajo del stock mínimo.")
    else:
        st.warning("Hay productos por reponer.")
        st.dataframe(low_df, use_container_width=True)


# ----------------------------- Inventario: Productos -----------------------------

def page_inventario_productos():
    st.title("📦 Productos")
    user = require_login()
    is_admin = (user['role'] == 'admin')

    if is_admin:
        with st.expander("➕ Crear / editar / eliminar producto", expanded=False):
            mode = st.radio("Acción", ["Crear","Editar","Eliminar"], horizontal=True, key="prod_mode")
            categories = run_query("SELECT id, name FROM categories ORDER BY name")
            suppliers = run_query("SELECT id, name FROM suppliers ORDER BY name")

            if mode == "Crear":
                sku = st.text_input("SKU *")
                name = st.text_input("Nombre *")
                brand = st.text_input("Marca")
                model = st.text_input("Modelo")
                barcode = st.text_input("Código de barras")
                uom = st.text_input("Unidad (UND, CAJA, LTR…)")
                usage_type = st.selectbox("Tipo de uso *", ["Administrativo","Asistencial"], index=0)
                cat = st.selectbox("Categoría", [None] + categories['name'].tolist())
                sup = st.selectbox("Proveedor", [None] + suppliers['name'].tolist())
                cost = st.number_input("Costo unitario", min_value=0.0, value=0.0, step=0.01)
                min_stock = st.number_input("Stock mínimo", min_value=0.0, value=0.0, step=1.0)
                active = st.checkbox("Activo", value=True)
                if st.button("Guardar producto"):
                    if not sku or not name:
                        st.error("SKU y nombre son obligatorios.")
                    else:
                        cat_id = int(categories.loc[categories['name']==cat, 'id'].iloc[0]) if cat else None
                        sup_id = int(suppliers.loc[suppliers['name']==sup, 'id'].iloc[0]) if sup else None
                        try:
                            run_script(
                                "INSERT INTO products(sku,name,brand,model,barcode,uom,usage_type,category_id,supplier_id,unit_cost,min_stock,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (sku.strip(), name.strip(), brand.strip(), model.strip(), barcode.strip(), uom.strip(), usage_type, cat_id, sup_id, float(cost), float(min_stock), 1 if active else 0)
                            )
                            st.success("Producto creado.")
                        except sqlite3.IntegrityError:
                            st.error("SKU duplicado.")

            elif mode == "Editar":
                df = df_products()
                if df.empty:
                    st.info("No hay productos para editar.")
                else:
                    sel_name = st.selectbox("Selecciona producto", df['producto'])
                    row = df[df['producto']==sel_name].iloc[0]
                    new_vals = {}
                    new_vals['sku'] = st.text_input("SKU *", value=row['sku'])
                    new_vals['name'] = st.text_input("Nombre *", value=row['producto'])
                    new_vals['brand'] = st.text_input("Marca", value=row['marca'] or "")
                    new_vals['model'] = st.text_input("Modelo", value=row['modelo'] or "")
                    new_vals['barcode'] = st.text_input("Código barras", value=row['codigo_barras'] or "")
                    new_vals['uom'] = st.text_input("Unidad", value=row['unidad'] or "")
                    new_vals['usage_type'] = st.selectbox("Tipo de uso *", ["Administrativo","Asistencial"], index=(0 if (row.get('tipo','Administrativo')=='Administrativo') else 1))
                    categories = run_query("SELECT id,name FROM categories ORDER BY name")
                    suppliers = run_query("SELECT id,name FROM suppliers ORDER BY name")
                    new_cat = st.selectbox("Categoría", [None] + categories['name'].tolist(), index=(categories['name'].tolist().index(row['categoria'])+1 if pd.notna(row['categoria']) and row['categoria'] in categories['name'].tolist() else 0))
                    new_sup = st.selectbox("Proveedor", [None] + suppliers['name'].tolist(), index=(suppliers['name'].tolist().index(row['proveedor'])+1 if pd.notna(row['proveedor']) and row['proveedor'] in suppliers['name'].tolist() else 0))
                    new_cost = st.number_input("Costo unitario", min_value=0.0, value=float(row['costo_unit']), step=0.01)
                    new_min = st.number_input("Stock mínimo", min_value=0.0, value=float(row['stock_min']), step=1.0)
                    active_flag = st.checkbox("Activo", value=bool(row['active']))
                    if st.button("Actualizar"):
                        cat_id = int(categories.loc[categories['name']==new_cat, 'id'].iloc[0]) if new_cat else None
                        sup_id = int(suppliers.loc[suppliers['name']==new_sup, 'id'].iloc[0]) if new_sup else None
                        run_script(
                            "UPDATE products SET sku=?, name=?, brand=?, model=?, barcode=?, uom=?, usage_type=?, category_id=?, supplier_id=?, unit_cost=?, min_stock=?, active=? WHERE id=?",
                            (new_vals['sku'].strip(), new_vals['name'].strip(), new_vals['brand'].strip(), new_vals['model'].strip(), new_vals['barcode'].strip(), new_vals['uom'].strip(), new_vals['usage_type'], cat_id, sup_id, float(new_cost), float(new_min), 1 if active_flag else 0, int(row['id']))
                        )
                        log_audit("UPDATE","products", int(row['id']), require_login()['username'], f"SKU={new_vals['sku']}")
                        st.success("Producto actualizado.")

            elif mode == "Eliminar":
                df = df_products()
                if df.empty:
                    st.info("No hay productos para eliminar.")
                else:
                    sel_name = st.selectbox("Selecciona producto a eliminar", df['producto'])
                    if st.button("Eliminar definitivamente ⚠️"):
                        pid = int(df[df['producto']==sel_name]['id'].iloc[0])
                        run_script("DELETE FROM products WHERE id=?", (pid,))
                        log_audit("DELETE","products", pid, require_login()['username'], sel_name)
                        st.success("Producto eliminado.")

    st.subheader("Listado de productos")
    df = df_products()
    filtro_tipo = st.selectbox("Filtrar por tipo", ["(Todos)","Administrativo","Asistencial"], index=0)
    if filtro_tipo != "(Todos)" and not df.empty:
        df = df[df['tipo']==filtro_tipo]
    st.dataframe(df, use_container_width=True)

    if is_admin:
        st.divider()
        st.subheader("Catálogos: Categorías, Proveedores")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Categorías**")
            new_cat = st.text_input("Nueva categoría")
            if st.button("Agregar categoría") and new_cat:
                try:
                    run_script("INSERT INTO categories(name) VALUES(?)", (new_cat.strip(),))
                    st.success("Categoría agregada")
                except sqlite3.IntegrityError:
                    st.error("Ya existe.")
            st.dataframe(run_query("SELECT id,name FROM categories ORDER BY name"), use_container_width=True)
        with c2:
            st.markdown("**Proveedores**")
            col1, col2 = st.columns(2)
            with col1:
                sup_name = st.text_input("Nombre proveedor")
                sup_contact = st.text_input("Contacto")
            with col2:
                sup_email = st.text_input("Email")
                sup_phone = st.text_input("Teléfono")
            if st.button("Agregar proveedor") and sup_name:
                try:
                    run_script("INSERT INTO suppliers(name,contact,email,phone) VALUES(?,?,?,?)", (sup_name.strip(), sup_contact.strip(), sup_email.strip(), sup_phone.strip()))
                    st.success("Proveedor agregado")
                except sqlite3.IntegrityError:
                    st.error("El nombre ya existe.")
            st.dataframe(run_query("SELECT id,name,contact,email,phone FROM suppliers ORDER BY name"), use_container_width=True)


# ----------------------------- Inventario: Bodegas & Movimientos -----------------------------

def page_inventario_bodegas():
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.title("🏬 Bodegas")
    name = st.text_input("Nombre de bodega")
    loc = st.text_input("Ubicación")
    if st.button("Crear bodega") and name:
        try:
            run_script("INSERT INTO warehouses(name,location) VALUES(?,?)", (name.strip(), loc.strip()))
            st.success("Bodega creada")
        except sqlite3.IntegrityError:
            st.error("El nombre ya existe.")

    st.subheader("Listado")
    df = run_query("SELECT id, name, location FROM warehouses ORDER BY name")
    st.dataframe(df, use_container_width=True)


def page_inventario_movimientos():
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.title("🔁 Movimientos de inventario")

    prods = run_query("SELECT id, sku || ' – ' || name AS label FROM products WHERE active=1 ORDER BY name")
    bodegas = run_query("SELECT id, name FROM warehouses ORDER BY name")

    with st.form("mov_form"):
        t = st.selectbox("Tipo", ["ENTRADA","SALIDA","TRANSFERENCIA","AJUSTE"], index=0)
        prod_label = st.selectbox("Producto", prods['label'] if not prods.empty else [])
        qty = st.number_input("Cantidad", min_value=0.0, step=1.0)
        unit_cost = st.number_input("Costo unitario (para ENTRADA/AJUSTE)", min_value=0.0, step=0.01)
        col1, col2 = st.columns(2)
        from_wh = to_wh = None
        if t in ("SALIDA","TRANSFERENCIA"):
            from_wh = col1.selectbox("Desde bodega", bodegas['name'] if not bodegas.empty else [])
        if t in ("ENTRADA","TRANSFERENCIA","AJUSTE"):
            to_wh = col2.selectbox("Hacia bodega", bodegas['name'] if not bodegas.empty else [])
        reason = st.text_area("Observación / Motivo")
        submitted = st.form_submit_button("Registrar movimiento")

    if submitted:
        if prods.empty or bodegas.empty:
            st.error("Debe existir al menos un producto y una bodega.")
            return
        if qty <= 0:
            st.error("La cantidad debe ser mayor a 0.")
            return
        prod_id = int(prods.loc[prods['label']==prod_label, 'id'].iloc[0])
        from_id = int(bodegas.loc[bodegas['name']==from_wh, 'id'].iloc[0]) if from_wh else None
        to_id = int(bodegas.loc[bodegas['name']==to_wh, 'id'].iloc[0]) if to_wh else None
        user = require_login()
        now = datetime.utcnow().isoformat()
        run_script(
            "INSERT INTO movements(type,product_id,from_wh,to_wh,qty,unit_cost,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (t, prod_id, from_id, to_id, float(qty), float(unit_cost), reason, user['id'], now)
        )
        # afectar stock
        if t == "ENTRADA":
            adjust_stock(prod_id, to_id, qty)
        elif t == "SALIDA":
            adjust_stock(prod_id, from_id, -qty)
        elif t == "TRANSFERENCIA":
            adjust_stock(prod_id, from_id, -qty)
            adjust_stock(prod_id, to_id, qty)
        elif t == "AJUSTE":
            if to_id:
                adjust_stock(prod_id, to_id, qty)
            elif from_id:
                adjust_stock(prod_id, from_id, -qty)
        log_audit("INSERT","movements", None, user['username'], f"Tipo={t}; Prod={prod_id}; Qty={qty}")
        st.success("Movimiento registrado.")

    st.subheader("Kárdex / Movimientos recientes")
    mov = run_query(
        """
        SELECT m.id, m.created_at, m.type, p.name AS producto,
               wf.name AS desde, wt.name AS hacia, m.qty, m.unit_cost, m.reason
        FROM movements m
        JOIN products p ON p.id = m.product_id
        LEFT JOIN warehouses wf ON wf.id = m.from_wh
        LEFT JOIN warehouses wt ON wt.id = m.to_wh
        ORDER BY m.id DESC LIMIT 200
        """
    )
    st.dataframe(mov, use_container_width=True)

    st.subheader("Stock actual por bodega")
    st.dataframe(df_stock(), use_container_width=True)


# ----------------------------- Inventario: Activos fijos -----------------------------

def page_inventario_activos():
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.title("🖥️ Activos fijos")
    bodegas = run_query("SELECT id,name FROM warehouses ORDER BY name")

    with st.expander("➕ Crear / editar / eliminar activo", expanded=False):
        mode = st.radio("Acción", ["Crear","Editar","Eliminar"], horizontal=True, key="asset_mode")
        if mode == "Crear":
            code = st.text_input("Código *")
            name = st.text_input("Nombre *")
            serial = st.text_input("Serie")
            atype = st.text_input("Tipo (PC, Monitor, Herramienta…) ")
            brand = st.text_input("Marca")
            model = st.text_input("Modelo")
            wh = st.selectbox("Bodega", [None] + bodegas['name'].tolist())
            status = st.selectbox("Estado", ["Operativo","Mantenimiento","Baja","Asignado"], index=0)
            assigned = st.text_input("Asignado a (texto)")
            pdate = st.date_input("Fecha compra", value=None, format="YYYY-MM-DD")
            cost = st.number_input("Costo", min_value=0.0, step=0.01)
            notes = st.text_area("Notas")
            active = st.checkbox("Activo (vigente)", value=True)
            if st.button("Guardar activo"):
                if not code or not name:
                    st.error("Código y nombre son obligatorios.")
                else:
                    wh_id = int(bodegas.loc[bodegas['name']==wh,'id'].iloc[0]) if wh else None
                    run_script(
                        "INSERT INTO assets(code,name,serial,type,brand,model,warehouse_id,status,assigned_to,purchase_date,cost,notes,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code.strip(), name.strip(), serial.strip(), atype.strip(), brand.strip(), model.strip(), wh_id, status, assigned.strip(), (pdate.isoformat() if pdate else None), float(cost), notes.strip(), 1 if active else 0)
                    )
                    st.success("Activo creado.")

        elif mode == "Editar":
            df = run_query("SELECT id, code, name FROM assets ORDER BY code")
            if df.empty:
                st.info("No hay activos para editar.")
            else:
                sel = st.selectbox("Selecciona activo", df['code'] + " – " + df['name'])
                idx_list = df.index[(df['code'] + " – " + df['name']) == sel].tolist()
                ridx = idx_list[0] if idx_list else 0
                full = run_query("SELECT * FROM assets WHERE id=?", (int(df.iloc[ridx]['id']),)).iloc[0]
                code = st.text_input("Código *", value=full['code'])
                name = st.text_input("Nombre *", value=full['name'])
                serial = st.text_input("Serie", value=full['serial'] or "")
                atype = st.text_input("Tipo", value=full['type'] or "")
                brand = st.text_input("Marca", value=full['brand'] or "")
                model = st.text_input("Modelo", value=full['model'] or "")
                wh_names = [None] + bodegas['name'].tolist()
                wh_index = 0
                if pd.notna(full['warehouse_id']):
                    wh_name_row = run_query("SELECT name FROM warehouses WHERE id=?", (full['warehouse_id'],))
                    wh_name = wh_name_row.loc[0,'name'] if not wh_name_row.empty else None
                    if wh_name in bodegas['name'].tolist():
                        wh_index = bodegas['name'].tolist().index(wh_name) + 1
                wh = st.selectbox("Bodega", wh_names, index=wh_index)
                status = st.selectbox("Estado", ["Operativo","Mantenimiento","Baja","Asignado"], index=["Operativo","Mantenimiento","Baja","Asignado"].index(full['status']))
                assigned = st.text_input("Asignado a", value=full['assigned_to'] or "")
                pdate_str = full['purchase_date'] or None
                pdate = None
                if pdate_str:
                    try:
                        pdate = datetime.fromisoformat(pdate_str).date()
                    except Exception:
                        pdate = None
                pdate = st.date_input("Fecha compra", value=pdate, format="YYYY-MM-DD")
                cost = st.number_input("Costo", min_value=0.0, value=float(full['cost'] or 0), step=0.01)
                notes = st.text_area("Notas", value=full['notes'] or "")
                active = st.checkbox("Activo (vigente)", value=bool(full['active']))
                if st.button("Actualizar"):
                    wh_id = int(bodegas.loc[bodegas['name']==wh,'id'].iloc[0]) if wh else None
                    run_script(
                        "UPDATE assets SET code=?, name=?, serial=?, type=?, brand=?, model=?, warehouse_id=?, status=?, assigned_to=?, purchase_date=?, cost=?, notes=?, active=? WHERE id=?",
                        (code.strip(), name.strip(), serial.strip(), atype.strip(), brand.strip(), model.strip(), wh_id, status, assigned.strip(), (pdate.isoformat() if pdate else None), float(cost), notes.strip(), 1 if active else 0, int(full['id']))
                    )
                    log_audit("UPDATE","assets", int(full['id']), require_login()['username'], f"code={code}")
                    st.success("Activo actualizado.")

        else:  # Eliminar
            df = run_query("SELECT id, code, name FROM assets ORDER BY code")
            if df.empty:
                st.info("No hay activos para eliminar.")
            else:
                sel = st.selectbox("Selecciona activo a eliminar", df['code'] + " – " + df['name'])
                idx_list = df.index[(df['code'] + " – " + df['name']) == sel].tolist()
                rid = int(df.iloc[idx_list[0]]['id']) if idx_list else int(df.iloc[0]['id'])
                if st.button("Eliminar definitivamente ⚠️"):
                    run_script("DELETE FROM assets WHERE id=?", (rid,))
                    log_audit("DELETE","assets", rid, require_login()['username'], sel)
                    st.success("Activo eliminado.")

    st.subheader("Listado de activos")
    adf = run_query(
        """
        SELECT a.id, a.code AS codigo, a.name AS nombre, a.serial AS serie, a.type AS tipo,
               a.brand AS marca, a.model AS modelo, w.name AS bodega, a.status, a.assigned_to AS asignado,
               a.purchase_date AS fecha_compra, a.cost AS costo, a.notes AS notas, a.active
        FROM assets a
        LEFT JOIN warehouses w ON w.id = a.warehouse_id
        ORDER BY a.code
        """
    )
    st.dataframe(adf, use_container_width=True)

    c1,c2 = st.columns(2)
    with c1:
        st.download_button("⬇️ CSV Activos", adf.to_csv(index=False).encode('utf-8'), file_name="activos.csv", mime="text/csv")
    with c2:
        excel_buf = io.BytesIO()
        with pd.ExcelWriter(excel_buf, engine='xlsxwriter') as writer:
            adf.to_excel(writer, sheet_name='activos', index=False)
        st.download_button("⬇️ XLSX Activos", excel_buf.getvalue(), file_name="activos.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ----------------------------- Tickets -----------------------------

def generate_ticket_code():
    today = datetime.utcnow().strftime('%Y%m%d')
    cnt = run_query("SELECT COUNT(*) AS n FROM tickets WHERE DATE(created_at) = DATE('now')")
    seq = int(cnt.loc[0,'n']) + 1
    return f"TCK-{today}-{seq:04d}"


def page_tickets_nuevo():
    st.title("🎫 Nuevo Ticket")
    user = require_login()

    products = run_query("SELECT id, name FROM products WHERE active=1 ORDER BY name")
    warehouses = run_query("SELECT id, name FROM warehouses ORDER BY name")
    assets = run_query("SELECT id, code || ' – ' || name AS label FROM assets WHERE active=1 ORDER BY code")

    with st.form("new_ticket"):
        title = st.text_input("Título *")
        desc = st.text_area("Descripción")
        cat = st.selectbox("Categoría", ["Incidente","Solicitud","Ajuste","Consulta"])
        prio = st.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"], index=1)
        sla = st.number_input("SLA (horas)", min_value=1, value=48, step=1)
        col1, col2 = st.columns(2)
        wh_name = col1.selectbox("Bodega relacionada", [None] + warehouses['name'].tolist())
        prod_name = col2.selectbox("Producto relacionado", [None] + products['name'].tolist())
        asset_label = st.selectbox("Activo relacionado", [None] + assets['label'].tolist())
        watchers = st.text_input("Correos watchers (separados por coma)")
        attachment = st.file_uploader("Adjunto (opcional)")
        submitted = st.form_submit_button("Crear Ticket")

    if submitted:
        if not title:
            st.error("El título es obligatorio.")
            return
        code = generate_ticket_code()
        now = datetime.utcnow()
        due = now + timedelta(hours=int(sla))
        wh_id = int(warehouses.loc[warehouses['name']==wh_name,'id'].iloc[0]) if wh_name else None
        prod_id = int(products.loc[products['name']==prod_name,'id'].iloc[0]) if prod_name else None
        asset_id = int(assets.loc[assets['label']==asset_label,'id'].iloc[0]) if asset_label else None
        attach_path = None
        if attachment is not None:
            tdir = os.path.join(UPLOAD_DIR, code)
            os.makedirs(tdir, exist_ok=True)
            fpath = os.path.join(tdir, attachment.name)
            with open(fpath, 'wb') as f:
                f.write(attachment.read())
            attach_path = fpath
        run_script(
            """
            INSERT INTO tickets(code,title,description,category,priority,status,sla_hours,created_by,assigned_to,warehouse_id,product_id,asset_id,watchers_emails,attachment_path,created_at,updated_at,due_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                code, title.strip(), desc, cat, prio, "Abierto", int(sla), user['id'], None, wh_id, prod_id, asset_id, watchers, attach_path,
                now.isoformat(), now.isoformat(), due.isoformat()
            )
        )
        log_audit("INSERT","tickets", None, user['username'], f"{code} creado")
        creator_email = run_query("SELECT email FROM users WHERE id=?", (user['id'],)).loc[0,'email'] if user['id'] else None
        notify_ticket_created(code, title, creator_email, None, [e.strip() for e in (watchers or "").split(',') if e.strip()])
        st.success(f"Ticket {code} creado correctamente.")


def page_tickets_bandeja(mis=False):
    user = require_login()
    role = user['role']
    st.title("📥 Bandeja de Tickets" if not mis else "📌 Mis Tickets")

    col1, col2, col3, col4 = st.columns(4)
    estado = col1.multiselect("Estado", ["Abierto","En Progreso","Resuelto","Cerrado"], default=["Abierto","En Progreso"])
    prio = col2.multiselect("Prioridad", ["Baja","Media","Alta","Crítica"], default=["Media","Alta","Crítica"])
    texto = col3.text_input("Buscar (título/desc)")
    ver_vencidos = col4.checkbox("Solo vencidos")

    sql = "SELECT t.*, u.username as creador, a.username as asignado, u.email as creador_email, a.email as asignado_email FROM tickets t LEFT JOIN users u ON u.id=t.created_by LEFT JOIN users a ON a.id=t.assigned_to WHERE 1=1"
    params = []
    if estado:
        sql += f" AND t.status IN ({','.join(['?']*len(estado))})"
        params += estado
    if prio:
        sql += f" AND t.priority IN ({','.join(['?']*len(prio))})"
        params += prio
    if texto:
        sql += " AND (t.title LIKE ? OR t.description LIKE ?)"
        params += [f"%{texto}%", f"%{texto}%"]
    if ver_vencidos:
        sql += " AND t.due_at IS NOT NULL AND t.due_at < ? AND t.status IN ('Abierto','En Progreso')"
        params += [datetime.utcnow().isoformat()]
    if mis:
        sql += " AND (t.created_by=? OR t.assigned_to=?)"
        params += [user['id'], user['id']]
    # Privacidad: los 'visor' solo pueden ver sus propios tickets
    if role == 'visor' and not mis:
        sql += " AND t.created_by=?"
        params += [user['id']]

    sql += " ORDER BY t.updated_at DESC LIMIT 500"
    df = run_query(sql, tuple(params))

    if df.empty:
        st.info("No hay tickets que cumplan el filtro.")
        return

    st.dataframe(df[["code","title","priority","status","due_at","creador","asignado","updated_at"]], use_container_width=True)

    st.divider()
    st.subheader("Gestionar ticket")
    codes = df['code'].tolist()
    sel = st.selectbox("Selecciona ticket", codes)
    if not sel:
        return

    tdf = df[df['code']==sel].iloc[0]
    st.markdown(f"**{tdf['title']}**  ")
    st.caption(f"Estado: {tdf['status']} · Prioridad: {tdf['priority']} · SLA: {tdf['sla_hours']}h · Vence: {tdf['due_at']}")
    st.write(tdf['description'] or "(Sin descripción)")
    if pd.notna(tdf['attachment_path']) and tdf['attachment_path']:
        try:
            with open(tdf['attachment_path'], 'rb') as f:
                b = f.read()
            st.download_button("⬇️ Descargar adjunto", b, file_name=os.path.basename(tdf['attachment_path']))
        except FileNotFoundError:
            st.warning("Adjunto no encontrado en el disco.")

    # Permisos
    is_admin = (role == 'admin')
    is_agent = (role == 'agente')
    is_visor = (role == 'visor')
    assigned_to_me = (pd.notna(tdf['assigned_to']) and int(tdf['assigned_to']) == user['id'])
    unassigned = pd.isna(tdf['assigned_to']) or (str(tdf['assigned_to']).strip() == "" )

    can_edit_status = is_admin or (is_agent and assigned_to_me)
    can_adjust_sla = is_admin or (is_agent and assigned_to_me)
    can_update_watchers = can_edit_status
    can_assign_admin = is_admin
    can_self_assign = is_agent and (unassigned or assigned_to_me)
    can_add_comment = is_admin or is_agent or (pd.notna(tdf['created_by']) and int(tdf['created_by']) == user['id'])

    roles_opts = run_query("SELECT id, username, email FROM users WHERE active=1 ORDER BY username")
    # Controles condicionales
    colA, colB, colC = st.columns(3)
    if can_edit_status:
        new_status = colA.selectbox("Cambiar estado", ["Abierto","En Progreso","Resuelto","Cerrado"], index=["Abierto","En Progreso","Resuelto","Cerrado"].index(tdf['status']))
    else:
        colA.text_input("Estado (solo lectura)", value=tdf['status'], disabled=True)
        new_status = tdf['status']

    # Asignación
    assignee = tdf['asignado'] if pd.notna(tdf['asignado']) else None
    assignee_email = tdf.get('asignado_email', None) if pd.notna(tdf.get('asignado_email', None)) else None
    if can_assign_admin:
        assignee = colB.selectbox("Asignar a", [None] + roles_opts['username'].tolist(), index=(roles_opts['username'].tolist().index(tdf['asignado'])+1 if pd.notna(tdf['asignado']) and tdf['asignado'] in roles_opts['username'].tolist() else 0))
        assignee_email = str(roles_opts.loc[roles_opts['username']==assignee,'email'].iloc[0]) if assignee else None
    elif can_self_assign:
        options = [None, user['username']]
        default_index = options.index(tdf['asignado']) if pd.notna(tdf['asignado']) and tdf['asignado'] in options else 0
        assignee = colB.selectbox("Asignar a (solo tú)", options, index=default_index)
        assignee_email = user['email'] if assignee == user['username'] else None
    else:
        colB.text_input("Asignado a (solo lectura)", value=(tdf['asignado'] or ""), disabled=True)

    if can_adjust_sla:
        more_hours = colC.number_input("Ajustar SLA (horas)", min_value=0, value=0)
    else:
        colC.number_input("Ajustar SLA (horas)", min_value=0, value=0, disabled=True)
        more_hours = 0

    watchers = st.text_input("Watchers (coma)", value=tdf.get('watchers_emails','') or '', disabled=not can_update_watchers)

    comment = st.text_area("Agregar comentario", disabled=not can_add_comment)

    # Guardar cambios (solo si hay permisos de edición)
    if (can_edit_status or can_assign_admin or can_self_assign or can_adjust_sla) and st.button("Guardar cambios"):
        now = datetime.utcnow()
        # Resolver IDs de asignación
        assignee_id = None
        assignee_email_eff = None
        if assignee:
            # Si admin: viene de roles_opts; si agente: puede ser self
            if assignee == user['username']:
                assignee_id = user['id']
                assignee_email_eff = user['email']
            else:
                # buscar en roles_opts
                if assignee in roles_opts['username'].tolist():
                    assignee_id = int(roles_opts.loc[roles_opts['username']==assignee,'id'].iloc[0])
                    assignee_email_eff = str(roles_opts.loc[roles_opts['username']==assignee,'email'].iloc[0])
        # Calcular due_at
        due_at = tdf['due_at']
        if more_hours > 0 and can_adjust_sla:
            try:
                due_at = (datetime.fromisoformat(due_at) + timedelta(hours=int(more_hours))).isoformat() if due_at else (now + timedelta(hours=int(more_hours))).isoformat()
            except Exception:
                due_at = (now + timedelta(hours=int(more_hours))).isoformat()
        closed_at = tdf['closed_at']
        if new_status == "Cerrado" and can_edit_status:
            closed_at = now.isoformat()
        # Actualizar
        run_script(
            "UPDATE tickets SET status=?, assigned_to=?, updated_at=?, due_at=?, closed_at=?, watchers_emails=? WHERE id=?",
            (new_status, assignee_id, now.isoformat(), due_at, closed_at, watchers if can_update_watchers else tdf.get('watchers_emails',''), int(tdf['id']))
        )
        if comment and can_add_comment:
            run_script("INSERT INTO ticket_comments(ticket_id,author_id,comment,created_at) VALUES(?,?,?,?)", (int(tdf['id']), require_login()['id'], comment, now.isoformat()))
        log_audit("UPDATE","tickets", int(tdf['id']), require_login()['username'], f"Estado={new_status}; Asignado={assignee}")
        # Notificaciones
        creator_email = tdf.get('creador_email','')
        if assignee:
            notify_ticket_assigned(tdf['code'], tdf['title'], creator_email, assignee, assignee_email_eff, [e.strip() for e in (watchers or '').split(',') if e.strip()])
            notify_agent_assigned(tdf['code'], tdf['title'], assignee_email_eff)
        if new_status == "Resuelto" and can_edit_status:
            notify_ticket_resolved(tdf['code'], tdf['title'], creator_email, [e.strip() for e in (watchers or '').split(',') if e.strip()])
        st.success("Ticket actualizado.")
        st.rerun()
    elif not (can_edit_status or can_assign_admin or can_self_assign or can_adjust_sla):
        st.info("No tienes permisos para editar este ticket. Puedes agregar comentarios si corresponde a tu rol.")

    st.subheader("Comentarios")
    cdf = run_query("SELECT c.created_at, u.username AS autor, c.comment FROM ticket_comments c LEFT JOIN users u ON u.id=c.author_id WHERE c.ticket_id=? ORDER BY c.id DESC", (int(tdf['id']),))
    st.dataframe(cdf, use_container_width=True)


# ----------------------------- Reportes / Exportar -----------------------------

def page_reportes_export():
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.title("📤 Reportes / Exportar")

    st.subheader("Inventario consolidado")
    inv = df_stock()
    st.dataframe(inv, use_container_width=True)
    st.download_button("⬇️ CSV Inventario", inv.to_csv(index=False).encode('utf-8'), file_name="inventario.csv", mime="text/csv")

    st.subheader("Tickets")
    t = run_query("SELECT * FROM tickets ORDER BY updated_at DESC")
    st.dataframe(t, use_container_width=True)

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine='xlsxwriter') as w:
        inv.to_excel(w, sheet_name='inventario', index=False)
        t.to_excel(w, sheet_name='tickets', index=False)
    st.download_button("⬇️ XLSX Inventario+Tickets", excel_buf.getvalue(), file_name="reportes_inventario_tickets.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.subheader("Kárdex completo")
    mov = run_query("SELECT m.created_at, m.type, p.sku, p.name AS producto, wf.name AS desde, wt.name AS hacia, m.qty, m.unit_cost, m.reason FROM movements m JOIN products p ON p.id=m.product_id LEFT JOIN warehouses wf ON wf.id=m.from_wh LEFT JOIN warehouses wt ON wt.id=m.to_wh ORDER BY m.id DESC")
    st.dataframe(mov, use_container_width=True)
    st.download_button("⬇️ CSV Kardex", mov.to_csv(index=False).encode('utf-8'), file_name="kardex.csv", mime="text/csv")

    st.subheader("Auditoría")
    audit = run_query("SELECT created_at, user, action, table_name, record_id, details FROM audit_log ORDER BY id DESC LIMIT 500")
    st.dataframe(audit, use_container_width=True)
    st.download_button("⬇️ CSV Auditoría", audit.to_csv(index=False).encode('utf-8'), file_name="auditoria.csv", mime="text/csv")


# ----------------------------- Configuración -----------------------------

def page_config_usuarios():
    st.title("⚙️ Configuración – Usuarios")
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.subheader("Crear usuario")
    with st.form("new_user"):
        uname = st.text_input("Usuario *")
        email = st.text_input("Email")
        role = st.selectbox("Rol", ["admin","agente","visor"], index=1)
        pwd1 = st.text_input("Contraseña *", type="password")
        pwd2 = st.text_input("Confirmar contraseña *", type="password")
        submitted = st.form_submit_button("Crear")
    if submitted:
        if not uname or not pwd1 or not pwd2:
            st.error("Campos obligatorios.")
        elif pwd1 != pwd2:
            st.error("Las contraseñas no coinciden.")
        else:
            salt = secrets.token_hex(8)
            try:
                run_script("INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)", (uname.strip(), email.strip(), hash_password(pwd1, salt), salt, role, datetime.utcnow().isoformat()))
                st.success("Usuario creado.")
            except sqlite3.IntegrityError:
                st.error("Usuario ya existe.")

    st.subheader("Usuarios existentes")
    df = run_query("SELECT id, username, email, role, active, created_at FROM users ORDER BY username")
    st.dataframe(df, use_container_width=True)

    st.subheader("Cambiar contraseña propia")
    with st.form("pwd_form"):
        oldp = st.text_input("Contraseña actual", type="password")
        newp = st.text_input("Nueva contraseña", type="password")
        submitted2 = st.form_submit_button("Actualizar contraseña")
    if submitted2:
        me = run_query("SELECT * FROM users WHERE id=?", (user['id'],)).iloc[0]
        if hash_password(oldp, me['password_salt']) != me['password_hash']:
            st.error("Contraseña actual incorrecta.")
        else:
            salt = secrets.token_hex(8)
            run_script("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(newp, salt), salt, user['id']))
            st.success("Contraseña actualizada.")


def page_config_notificaciones():
    st.title("📧 Configuración – Notificaciones (SMTP)")
    if require_login()['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.info("Puedes definir la contraseña vía variable de entorno **APP_SMTP_PASSWORD** para mayor seguridad. Si la escribes aquí, se almacenará en SQLite.")

    with st.form("smtp_form"):
        server = st.text_input("Servidor SMTP", value=get_setting("smtp_server",""))
        port = st.number_input("Puerto", min_value=1, value=int(get_setting("smtp_port","587") or 587))
        use_tls = st.checkbox("Usar TLS", value=get_setting("smtp_use_tls","1")=="1")
        username = st.text_input("Usuario SMTP", value=get_setting("smtp_username",""))
        password = st.text_input("Contraseña SMTP (opcional si usas APP_SMTP_PASSWORD)", type="password", value=get_setting("smtp_password",""))
        from_addr = st.text_input("Remitente (From)", value=get_setting("smtp_from", username or ""))
        notif_create = st.checkbox("Notificar en creación de ticket", value=get_setting("notif_on_create","1")=="1")
        notif_resolve = st.checkbox("Notificar en resolución de ticket", value=get_setting("notif_on_resolve","1")=="1")
        default_to = st.text_input("Correos por defecto (coma)", value=get_setting("notif_default_to",""))
        submitted = st.form_submit_button("Guardar configuración")

    if submitted:
        set_setting("smtp_server", server.strip())
        set_setting("smtp_port", str(int(port)))
        set_setting("smtp_use_tls", "1" if use_tls else "0")
        set_setting("smtp_username", username.strip())
        if password:
            set_setting("smtp_password", password)
        set_setting("smtp_from", from_addr.strip())
        set_setting("notif_on_create", "1" if notif_create else "0")
        set_setting("notif_on_resolve", "1" if notif_resolve else "0")
        set_setting("notif_default_to", default_to.strip())
        st.success("Configuración guardada.")

    st.divider()
    st.subheader("Probar envío")
    to = st.text_input("Enviar correo de prueba a:")
    if st.button("Enviar prueba"):
        ok = send_email([to], "Prueba SMTP – Mesa de Ayuda", "<p>Mensaje de prueba correcto.</p>")
        st.success("Correo enviado.") if ok else st.error("Error enviando correo.")


# ----------------------------- Registro (Self‑Signup) -----------------------------

def _username_available(u: str) -> bool:
    return run_query("SELECT 1 FROM users WHERE username=?", (u,)).empty


def signup_ui():
    st.header("🆕 Crear cuenta")
    st.caption("Se creará una cuenta activa con rol **visor**. Podrás cambiar el rol en Configuración → Usuarios (solo admin).")

    with st.form("signup_form"):
        full_name = st.text_input("Nombre completo")
        email = st.text_input("Email *")
        submitted = st.form_submit_button("Crear mi cuenta")

    if submitted:
        if not email or "@" not in email:
            st.error("Email inválido.")
            return
        base = (email.split('@')[0] or _slug(full_name))
        username = _slug(base)
        # asegurar unicidad
        if not _username_available(username):
            suffix = 1
            while not _username_available(f"{username}{suffix}") and suffix < 9999:
                suffix += 1
            username = f"{username}{suffix}"
        pwd_plain = _generate_password()
        salt = secrets.token_hex(8)
        run_script(
            "INSERT INTO users(username,email,password_hash,password_salt,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
            (username, email.strip(), hash_password(pwd_plain, salt), salt, "visor", datetime.utcnow().isoformat())
        )
        st.success("✅ Cuenta creada.")
        st.info(f"**Tu usuario:** `{username}`  \n**Tu contraseña:** `{pwd_plain}`  \nGuárdala ahora; no se puede recuperar luego (solo cambiar).")
        # Enviar por correo si SMTP configurado
        if _smtp_enabled():
            send_email([email.strip()], "Credenciales de acceso – Mesa de Ayuda", f"<p>Usuario: <b>{username}</b><br/>Contraseña: <b>{pwd_plain}</b></p>")

    if st.button("⬅️ Volver a Iniciar sesión"):
        st.session_state["mode"] = "login"
        st.rerun()


# ----------------------------- Sidebar / Router -----------------------------

def sidebar_menu():
    user = require_login()
    st.sidebar.title("Navegación")
    st.sidebar.write(f"👤 **{user['username']}** · Rol: *{user['role']}*")

    role = user['role']
    if role == 'admin':
        items = [
            "Dashboard",
            "Inventario – Productos",
            "Inventario – Bodegas",
            "Inventario – Movimientos",
            "Inventario – Reposiciones",
            "Inventario – Activos",
            "Tickets – Nuevo",
            "Tickets – Bandeja",
            "Tickets – Mis Tickets",
            "Reportes / Exportar",
            "Configuración – Usuarios",
            "Configuración – Notificaciones",
        ]
    elif role == 'agente':
        items = [
            "Dashboard",
            "Tickets – Nuevo",
            "Tickets – Bandeja",
            "Tickets – Mis Tickets",
        ]
    else:  # visor
        items = [
            "Dashboard",
            "Tickets – Nuevo",
            "Tickets – Mis Tickets",
        ]

    menu = st.sidebar.radio("Ir a", items, index=0)

    if st.sidebar.button("Cerrar sesión"):
        st.session_state.pop(SESSION_USER_KEY, None)
        st.rerun()

    return menu


def page_inventario_reposiciones():
    user = require_login()
    if user['role'] != 'admin':
        st.error("Solo administradores.")
        return

    st.title("🧾 Reposiciones / Bajo stock")
    df = run_query(
        """
        SELECT p.id, p.sku, p.name AS producto, p.min_stock,
               COALESCE(SUM(s.qty),0) AS stock_total
        FROM products p
        LEFT JOIN stock s ON s.product_id = p.id
        GROUP BY p.id
        HAVING stock_total < p.min_stock
        ORDER BY stock_total ASC
        """
    )
    if df.empty:
        st.success("No hay productos por debajo del mínimo.")
    else:
        st.warning("Productos por reponer")
        st.dataframe(df, use_container_width=True)
        df['sugerido_compra'] = (df['min_stock'] - df['stock_total']).clip(lower=0)
        st.download_button("⬇️ Descargar sugerido (CSV)", df.to_csv(index=False).encode('utf-8'), file_name="sugerido_reposicion.csv", mime="text/csv")


# ----------------------------- Main -----------------------------

def router():
    user = require_login()
    if not user:
        login_ui()
        return
    page = sidebar_menu()
    if page == "Dashboard":
        page_dashboard()
    elif page == "Inventario – Productos":
        page_inventario_productos()
    elif page == "Inventario – Bodegas":
        page_inventario_bodegas()
    elif page == "Inventario – Movimientos":
        page_inventario_movimientos()
    elif page == "Inventario – Reposiciones":
        page_inventario_reposiciones()
    elif page == "Inventario – Activos":
        page_inventario_activos()
    elif page == "Tickets – Nuevo":
        page_tickets_nuevo()
    elif page == "Tickets – Bandeja":
        page_tickets_bandeja(mis=False)
    elif page == "Tickets – Mis Tickets":
        page_tickets_bandeja(mis=True)
    elif page == "Reportes / Exportar":
        page_reportes_export()
    elif page == "Configuración – Usuarios":
        page_config_usuarios()
    elif page == "Configuración – Notificaciones":
        page_config_notificaciones()


def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="📦", layout="wide")
    init_db()
    router()


if __name__ == "__main__":
    main()
