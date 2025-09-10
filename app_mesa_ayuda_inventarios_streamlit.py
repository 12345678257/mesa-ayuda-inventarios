
import os, sqlite3, smtplib, ssl, random, string
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date
from dateutil import tz
import pandas as pd
import streamlit as st

APP_DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.getcwd(), "inventarios_helpdesk.db"))

# ----------------- Utilidades de seguridad -----------------
def _pbkdf2_hash(password: str, salt: str) -> str:
    import hashlib, binascii
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return binascii.hexlify(dk).decode()

def hash_password(password: str) -> str:
    salt = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    return salt + "$" + _pbkdf2_hash(password, salt)

def verify_password(password: str, salted_hash: str) -> bool:
    try:
        salt, h = salted_hash.split("$", 1)
        return _pbkdf2_hash(password, salt) == h
    except Exception:
        return False

def send_email(to_email: str, subject: str, body: str):
    host = os.getenv("SMTP_HOST"); user = os.getenv("SMTP_USER"); pwd = os.getenv("SMTP_PASSWORD"); port = int(os.getenv("SMTP_PORT", "587"))
    if not (host and user and pwd and to_email):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.starttls(context=context)
            server.login(user, pwd)
            server.send_message(msg)
        return True
    except Exception:
        return False

# ----------------- DB -----------------
def get_cx():
    cx = sqlite3.connect(APP_DB_PATH, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    return cx

INIT_SQL = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE,
  password TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','agente','usuario')),
  active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token TEXT UNIQUE NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT NOT NULL CHECK(category IN ('Incidente','Solicitud','Ajuste','Consulta')),
  itil_type TEXT NOT NULL CHECK(itil_type IN ('Incidente','Solicitud','Cambio','Problema')),
  priority TEXT,
  urgency TEXT,
  impact TEXT,
  status TEXT NOT NULL,
  created_by INTEGER NOT NULL,
  assigned_to INTEGER,
  watchers_emails TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  due_at TEXT,
  warehouse_id INTEGER,
  product_id INTEGER,
  asset_id INTEGER,
  ci_id INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  changed_by INTEGER NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  name TEXT NOT NULL,
  category TEXT,
  serial TEXT,
  acquisition_cost REAL DEFAULT 0,
  acquisition_date TEXT,
  useful_life_years INTEGER,
  depreciation_method TEXT DEFAULT 'linea_recta',
  salvage_value REAL DEFAULT 0,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS asset_maintenances(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  maintenance_type TEXT,
  description TEXT,
  cost REAL DEFAULT 0,
  performed_by TEXT,
  performed_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS asset_assignments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  user_id INTEGER,
  location TEXT,
  assigned_at TEXT NOT NULL,
  returned_at TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS ci_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  ci_type TEXT
);
CREATE TABLE IF NOT EXISTS ci_relations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_ci_id INTEGER NOT NULL,
  child_ci_id INTEGER NOT NULL,
  relation_type TEXT
);
CREATE TABLE IF NOT EXISTS kb_articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  title TEXT NOT NULL,
  body TEXT,
  tags TEXT
);
"""

def migrate_schema():
    cx = get_cx(); c = cx.cursor()
    for stmt in INIT_SQL.strip().split(");"):
        s = stmt.strip()
        if not s: continue
        c.execute(s + (");" if not s.endswith(");") else ""))
    cx.commit()
    # Asegurar columnas recientes (idempotente)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(assets)")} 
    if "acquisition_cost" not in cols:
        try: c.execute("ALTER TABLE assets ADD COLUMN acquisition_cost REAL DEFAULT 0"); cx.commit()
        except Exception: pass
    return cx

def run_query(sql, params=()):
    cx = migrate_schema()
    return pd.read_sql_query(sql, cx, params=params)

def run_script(sql, params=()):
    cx = migrate_schema()
    cx.execute(sql, params); cx.commit()

# ----------------- Sesión / Auth -----------------
def get_current_user():
    u = st.session_state.get("auth_user")
    return u

def login(username, password):
    df = run_query("SELECT * FROM users WHERE username=? AND active=1", (username,))
    if df.empty: return False
    row = df.iloc[0]
    if verify_password(password, row["password"]):
        st.session_state["auth_user"] = dict(row)
        return True
    return False

def ensure_admin_exists():
    df = run_query("SELECT COUNT(*) n FROM users")
    if int(df.loc[0,"n"]) == 0:
        pwd = "Admin1234"
        run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                   ("admin","admin@example.com",hash_password(pwd),"admin"))
        st.info(f"Se creó el usuario admin / {pwd} (cámbialo).")

# ----------------- Depreciación -----------------
def compute_depreciation(cost: float, salvage: float, life_years: int, acq_date: str, as_of=None):
    try:
        if as_of is None: as_of = date.today()
        if not acq_date: return 0.0, 0.0, cost, 0
        y,m,d = (int(x) for x in acq_date.split("-"))
        acq = date(y,m,d)
        months = max((as_of.year - acq.year)*12 + (as_of.month - acq.month), 0)
        base = max(cost - salvage, 0)
        total_months = max(life_years*12, 1)
        per_month = base/total_months
        acumulada = min(per_month*months, base)
        valor_libros = max(cost - acumulada, salvage)
        return round(per_month,2), round(acumulada,2), round(valor_libros,2), months
    except Exception:
        return 0.0, 0.0, cost, 0

# ----------------- UI -----------------
def sidebar_menu():
    user = get_current_user()
    if user:
        st.sidebar.markdown(f"**Conectado:** `{user['username']}` ({user['role']})")
        if st.sidebar.button("Cerrar sesión"):
            st.session_state.pop("auth_user", None)
            st.rerun()
    else:
        st.sidebar.info("No has iniciado sesión.")

    # Menú según rol
    base = ["Dashboard","Tickets – Nuevo","Tickets – Bandeja","Activos","CMDB","Mi Perfil y Seguridad","Configuración"]
    return st.sidebar.selectbox("Ir a:", base)

def page_login():
    st.title("Mesa de Ayuda + Inventarios — ITIL 4 (Enterprise+)")
    ensure_admin_exists()
    tab_login, tab_reg, tab_reset = st.tabs(["Iniciar sesión","Registrarse","Recuperar contraseña"])
    with tab_login:
        u = st.text_input("Usuario")
        p = st.text_input("Contraseña", type="password")
        if st.button("Entrar"):
            if login(u,p):
                st.success("Bienvenido.")
                st.rerun()
            else:
                st.error("Credenciales inválidas.")
    with tab_reg:
        u = st.text_input("Usuario nuevo")
        e = st.text_input("Email")
        p1 = st.text_input("Contraseña", type="password")
        p2 = st.text_input("Confirmar", type="password")
        if st.button("Crear cuenta"):
            if not u or not e or not p1 or p1!=p2 or len(p1)<8:
                st.error("Completa todos los campos y usa una contraseña de 8+ caracteres.")
            else:
                try:
                    run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                               (u,e,hash_password(p1),"usuario"))
                    st.success("Usuario creado. Ya puedes iniciar sesión.")
                except Exception as ex:
                    st.error("No se pudo crear (posible duplicado).")
    with tab_reset:
        e = st.text_input("Tu email")
        if st.button("Enviar enlace"):
            df = run_query("SELECT id FROM users WHERE email=?", (e,))
            if df.empty:
                st.error("Email no encontrado.")
            else:
                token = ''.join(random.choices(string.ascii_letters+string.digits,k=32))
                run_script("INSERT INTO password_resets(user_id,token,expires_at) VALUES(?,?,datetime('now','+1 hour'))",
                           (int(df.loc[0,"id"]), token))
                body = f"Para restablecer tu contraseña usa este token en la pantalla de recuperación: {token} (expira en 1h)"
                send_email(e, "Recuperación de contraseña", body)
                st.success("Si tu email existe, te enviamos el token.")

def page_dashboard():
    st.header("Dashboard")
    t = run_query("SELECT status, COUNT(*) n FROM tickets GROUP BY status ORDER BY 2 DESC")
    st.dataframe(t, use_container_width=True)

def _ticket_code():
    today_str = datetime.utcnow().strftime("%Y%m%d")
    df = run_query("SELECT COUNT(*) n FROM tickets WHERE substr(created_at,1,10)=substr(datetime('now'),1,10)")
    seq = int(df.loc[0,"n"]) + 1
    return f"TCK-{today_str}-{seq:04d}"

def page_tickets_nuevo():
    st.header("Nuevo ticket")
    title = st.text_input("Título")
    desc = st.text_area("Descripción")
    itil_type = st.selectbox("Tipo ITIL", ["Incidente","Solicitud","Cambio","Problema"])
    category = st.selectbox("Categoría (para reportes)", ["Incidente","Solicitud","Ajuste","Consulta"])
    priority = st.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"])
    urgency = st.selectbox("Urgencia", ["Baja","Media","Alta"])
    impact = st.selectbox("Impacto", ["Bajo","Medio","Alto"])
    owner = get_current_user()
    if st.button("Crear"):
        if not owner:
            st.error("Inicia sesión.")
            return
        code = _ticket_code()
        now = datetime.utcnow().isoformat()
        run_script("""INSERT INTO tickets(code,title,description,category,itil_type,priority,urgency,impact,status,created_by,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (code,title.strip(),desc.strip(),category,itil_type,priority,urgency,impact,"Nuevo",int(owner["id"]),now,now))
        # notificar
        if owner.get("email"):
            send_email(owner["email"], f"[{code}] Ticket creado", f"Tu ticket '{title}' fue creado con código {code}.")
        st.success(f"Creado: {code}")
        st.rerun()

def page_tickets_bandeja():
    st.header("Bandeja de tickets")
    tab_inc, tab_sol, tab_cam, tab_prob = st.tabs(["Incidentes","Solicitudes","Cambios","Problemas"])
    user = get_current_user()
    if user is None:
        st.warning("Inicia sesión.")
        return
    role = user["role"]
    def _grid(filtro):
        base = "SELECT t.id, t.code, t.title, t.status, t.itil_type, u.username AS owner FROM tickets t JOIN users u ON u.id=t.created_by WHERE t.itil_type=?"
        params = [filtro]
        if role == "usuario":
            base += " AND t.created_by=?"; params.append(int(user["id"]))
        df = run_query(base + " ORDER BY t.updated_at DESC", tuple(params))
        st.dataframe(df, use_container_width=True)
        sel = st.text_input(f"ID a abrir ({filtro})", "")
        if st.button(f"Abrir {filtro}") and sel.strip().isdigit():
            st.session_state["current_ticket_id"] = int(sel.strip())
            st.session_state["current_ticket_code"] = df[df["id"]==int(sel.strip())]["code"].iloc[0] if not df.empty else ""
            st.rerun()

    with tab_inc: _grid("Incidente")
    with tab_sol: _grid("Solicitud")
    with tab_cam: _grid("Cambio")
    with tab_prob: _grid("Problema")

def page_ticket_detalle():
    tid = st.session_state.get("current_ticket_id")
    if not tid:
        st.warning("Abre un ticket desde la bandeja.")
        return
    t = run_query("SELECT t.*, u.username as owner_name, u.email as owner_email FROM tickets t JOIN users u ON u.id=t.created_by WHERE t.id=?", (int(tid),))
    if t.empty:
        st.error("No encontrado.")
        return
    row = t.iloc[0]
    st.header(f"[{row['code']}] {row['title']}")
    st.caption(f"Propietario: {row['owner_name']} · Estado: {row['status']} · Tipo: {row['itil_type']}")
    st.write(row["description"] or "")

    user = get_current_user()
    if user and user["role"] in ("admin","agente"):
        st.subheader("Cambio de estado")
        new_status = st.selectbox("Nuevo estado", ["Nuevo","Asignado","En Progreso","En Espera","Resuelto","Cerrado"])
        if st.button("Aplicar estado"):
            now = datetime.utcnow().isoformat()
            run_script("UPDATE tickets SET status=?, updated_at=? WHERE id=?", (new_status, now, int(tid)))
            run_script("INSERT INTO ticket_status_history(ticket_id,status,changed_by,changed_at) VALUES(?,?,?,?)",
                       (int(tid), new_status, int(user["id"]), now))
            send_email(row["owner_email"], f"[{row['code']}] Estado: {new_status}",
                       f"Tu ticket {row['code']} cambió a estado: {new_status}.")
            st.success("Estado actualizado.")
            st.rerun()

    st.subheader("Historial de estados")
    h = run_query("SELECT h.status, h.changed_at, u.username AS by_user FROM ticket_status_history h JOIN users u ON u.id=h.changed_by WHERE h.ticket_id=? ORDER BY h.changed_at DESC", (int(tid),))
    st.dataframe(h, use_container_width=True)

def page_activos():
    st.header("Activos")
    # Crear rápido
    with st.form("new_asset"):
        c1, c2, c3 = st.columns(3)
        with c1: code = st.text_input("Código")
        with c2: name = st.text_input("Nombre")
        with c3: category = st.text_input("Categoría")
        c1,c2,c3,c4 = st.columns(4)
        with c1: serial = st.text_input("Serial")
        with c2: acq_date = st.date_input("Fecha adquisición", value=date.today())
        with c3: cost = st.number_input("Costo", min_value=0.0, step=0.01)
        with c4: life = st.number_input("Vida útil (años)", min_value=0, step=1)
        submitted = st.form_submit_button("Crear/Actualizar")
    if submitted and name.strip():
        if acq_date: acq_str = acq_date.isoformat()
        else: acq_str = None
        ex = run_query("SELECT id FROM assets WHERE code=?", (code,))
        if ex.empty:
            run_script("""INSERT INTO assets(code,name,category,serial,acquisition_cost,acquisition_date,useful_life_years)
                          VALUES(?,?,?,?,?,?,?)""", (code,name,category,serial,float(cost),acq_str,int(life)))
            st.success("Activo creado.")
        else:
            run_script("""UPDATE assets SET name=?, category=?, serial=?, acquisition_cost=?, acquisition_date=?, useful_life_years=?
                          WHERE code=?""", (name,category,serial,float(cost),acq_str,int(life),code))
            st.success("Activo actualizado.")

    a = run_query("SELECT id, code, name, category, acquisition_cost, acquisition_date, useful_life_years, salvage_value FROM assets ORDER BY name")
    st.dataframe(a, use_container_width=True)

    st.subheader("Detalle / Hoja de Vida / Depreciación")
    if not a.empty:
        sel = st.selectbox("Selecciona activo", a["code"])
        r = run_query("SELECT * FROM assets WHERE code=?", (sel,)).iloc[0]
        t1, t2, t3 = st.tabs(["Detalle","Hoja de Vida","Depreciación"])
        with t1:
            st.json(dict(r))
        with t2:
            st.write("Asignaciones")
            asg = run_query("SELECT * FROM asset_assignments WHERE asset_id=? ORDER BY assigned_at DESC", (int(r["id"]),))
            st.dataframe(asg, use_container_width=True)
            with st.form("form_asg"):
                loc = st.text_input("Ubicación/Área")
                notes = st.text_input("Notas")
                if st.form_submit_button("Registrar asignación"):
                    run_script("INSERT INTO asset_assignments(asset_id,location,assigned_at,notes) VALUES(?,?,datetime('now'),?)",
                               (int(r["id"]), loc, notes))
                    st.success("Asignación registrada.")
                    st.rerun()
            st.write("Mantenimientos")
            mt = run_query("SELECT * FROM asset_maintenances WHERE asset_id=? ORDER BY performed_at DESC", (int(r["id"]),))
            st.dataframe(mt, use_container_width=True)
            with st.form("form_maint"):
                mtype = st.text_input("Tipo")
                mdesc = st.text_area("Descripción")
                mcost = st.number_input("Costo", min_value=0.0, step=0.01)
                if st.form_submit_button("Registrar mantenimiento"):
                    run_script("INSERT INTO asset_maintenances(asset_id,maintenance_type,description,cost,performed_at) VALUES(?,?,?,?,datetime('now'))",
                               (int(r["id"]), mtype, mdesc, float(mcost)))
                    st.success("Mantenimiento registrado.")
                    st.rerun()
        with t3:
            pm, acc, vl, meses = compute_depreciation(float(r.get("acquisition_cost") or 0), float(r.get("salvage_value") or 0), int(r.get("useful_life_years") or 0), r.get("acquisition_date") or "")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Depreciación mensual", pm)
            c2.metric("Depreciación acumulada", acc)
            c3.metric("Valor en libros", vl)
            c4.metric("Meses transcurridos", meses)

def page_cmdb():
    st.header("CMDB")
    with st.form("ci_form"):
        name = st.text_input("Nombre CI")
        ci_type = st.text_input("Tipo CI (Servidor, App, DB, etc.)")
        if st.form_submit_button("Crear CI"):
            run_script("INSERT INTO ci_items(name,ci_type) VALUES(?,?)", (name,ci_type))
            st.success("CI creado.")
            st.rerun()
    cis = run_query("SELECT * FROM ci_items ORDER BY name")
    st.dataframe(cis, use_container_width=True)

    st.subheader("Relaciones CI")
    cis2 = run_query("SELECT id, name FROM ci_items ORDER BY name")
    if not cis2.empty:
        with st.form("ci_rel_form"):
            p = st.selectbox("CI Padre", cis2['name'])
            c = st.selectbox("CI Hijo", cis2['name'])
            r = st.text_input("Tipo de relación (depende de, usa, replica, etc.)")
            submitted2 = st.form_submit_button("Crear relación")
        if submitted2:
            pid = int(cis2.loc[cis2['name']==p,'id'].iloc[0])
            cid = int(cis2.loc[cis2['name']==c,'id'].iloc[0])
            if pid == cid:
                st.error("Padre y Hijo no pueden ser el mismo.")
            else:
                run_script("INSERT INTO ci_relations(parent_ci_id,child_ci_id,relation_type) VALUES(?,?,?)", (pid,cid,r.strip()))
                st.success("Relación creada.")
    rel = run_query("""SELECT pr.name AS padre, ch.name AS hijo, r.relation_type
                       FROM ci_relations r
                       JOIN ci_items pr ON pr.id=r.parent_ci_id
                       JOIN ci_items ch ON ch.id=r.child_ci_id
                       ORDER BY pr.name, ch.name""")
    st.dataframe(rel, use_container_width=True)

def page_configuracion():
    st.header("Configuración")
    t1,t2,t3 = st.tabs(["Seguridad","Notificaciones","SLA y Matrices"])
    user = get_current_user()
    if user is None or user["role"]!="admin":
        st.info("Solo administradores pueden modificar configuración.")
        return
    with t1:
        st.subheader("Usuarios y roles")
        u = run_query("SELECT id, username, email, role, active FROM users ORDER BY id")
        st.dataframe(u, use_container_width=True)
        with st.form("create_user"):
            c1,c2,c3,c4 = st.columns(4)
            with c1: ux = st.text_input("Usuario")
            with c2: ex = st.text_input("Email")
            with c3: rx = st.selectbox("Rol", ["admin","agente","usuario"])
            with c4: px = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Crear usuario"):
                run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                           (ux,ex,hash_password(px),rx))
                st.success("Usuario creado.")
                st.rerun()
    with t2:
        st.subheader("SMTP")
        st.write("Configura variables de entorno en la plataforma (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD).")
    with t3:
        st.subheader("Matriz urgencia × impacto (global) — demo")
        st.write("Puedes parametrizar esta matriz en una tabla dedicada (no incluida por brevedad).")

def router():
    user = get_current_user()
    if not user:
        page_login(); return
    page = sidebar_menu()
    if page == "Dashboard": page_dashboard()
    elif page == "Tickets – Nuevo": page_tickets_nuevo()
    elif page == "Tickets – Bandeja": page_tickets_bandeja()
    elif page == "Activos": page_activos()
    elif page == "CMDB": page_cmdb()
    elif page == "Mi Perfil y Seguridad": page_mi_perfil_seguridad()
    elif page == "Configuración": page_configuracion()
    else: st.stop()

def main():
    st.set_page_config(page_title="Mesa de Ayuda + Inventarios", layout="wide")
    router()

if __name__ == "__main__":
    main()
