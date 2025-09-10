
import os, sqlite3, smtplib, ssl, random, string, hmac, hashlib as _hashlib, json, requests, io, base64
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

APP_DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.getcwd(), "inventarios_helpdesk.db"))
SSO_SHARED_SECRET = os.getenv("SSO_SHARED_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --------- Estilos ---------
st.markdown("""
<style>
.small-muted { color:#64748b; font-size:0.9rem; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; background:#e2e8f0; margin-right:6px; }
.btn-primary button { background:#3B82F6 !important; color:white !important; border-radius:12px; }
.btn-success button { background:#16a34a !important; color:white !important; border-radius:12px; }
.btn-danger button { background:#dc2626 !important; color:white !important; border-radius:12px; }
.kpi { background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:12px 16px; }
</style>
""", unsafe_allow_html=True)

# --------- Seguridad ---------
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

def safe_filename(name: str) -> str:
    return str(name).replace("..","_").replace("/","_").replace("\\\\","_")

def send_email(to_email: str, subject: str, body: str):
    # Prioriza settings persistidos; fallback a variables de entorno
    host = get_setting("smtp_host", os.getenv("SMTP_HOST"))
    user = get_setting("smtp_user", os.getenv("SMTP_USER"))
    pwd  = get_setting("smtp_password", os.getenv("SMTP_PASSWORD"))
    port = int(get_setting("smtp_port", os.getenv("SMTP_PORT") or "587"))
    from_addr = get_setting("smtp_from", user or "")
    if not (host and user and pwd and to_email):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr or user
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

def _post_webhook(url, payload):
    try:
        if not url: return
        headers = {"Content-Type":"application/json"}
        requests.post(url, data=json.dumps(payload), headers=headers, timeout=5)
    except Exception:
        pass

def notify_webhooks(event_type: str, data: dict):
    payload = {"event": event_type, "data": data, "ts": datetime.utcnow().isoformat()}
    _post_webhook(SLACK_WEBHOOK_URL, payload)
    _post_webhook(TEAMS_WEBHOOK_URL, payload)
    _post_webhook(DISCORD_WEBHOOK_URL, payload)

# --------- DB ---------
def get_cx():
    cx = sqlite3.connect(APP_DB_PATH, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    return cx

INIT_SQL = """
-- Usuarios y equipos
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE,
  password TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','agente','usuario')),
  team_id INTEGER,
  active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL
);

-- Servicios, Áreas, Aprobaciones
CREATE TABLE IF NOT EXISTS services(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS areas(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS service_area_levels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  service_id INTEGER NOT NULL,
  area_id INTEGER NOT NULL,
  level INTEGER NOT NULL,
  UNIQUE(service_id, area_id, level)
);
CREATE TABLE IF NOT EXISTS change_approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  service_id INTEGER NOT NULL,
  area_id INTEGER NOT NULL,
  level INTEGER NOT NULL,
  approver_user_id INTEGER,
  status TEXT NOT NULL CHECK(status IN ('Pendiente','Aprobado','Rechazado')) DEFAULT 'Pendiente',
  decided_at TEXT,
  notes TEXT
);

-- Matriz Urgencia × Impacto por servicio y SLA
CREATE TABLE IF NOT EXISTS service_matrix(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  service_id INTEGER NOT NULL,
  urgency TEXT NOT NULL,
  impact TEXT NOT NULL,
  priority TEXT NOT NULL,
  UNIQUE(service_id, urgency, impact)
);
CREATE TABLE IF NOT EXISTS service_sla(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  service_id INTEGER NOT NULL,
  priority TEXT NOT NULL,
  response_hours INTEGER NOT NULL,
  resolve_hours INTEGER NOT NULL,
  UNIQUE(service_id, priority)
);

-- Tickets
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT NOT NULL CHECK(category IN ('Incidente','Solicitud','Ajuste','Consulta')),
  itil_type TEXT NOT NULL CHECK(itil_type IN ('Incidente','Solicitud','Cambio','Problema')),
  service_id INTEGER,
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
  response_due_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  changed_by INTEGER NOT NULL,
  changed_at TEXT NOT NULL
);

-- Encuestas
CREATE TABLE IF NOT EXISTS ticket_surveys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  survey_type TEXT NOT NULL CHECK(survey_type IN ('CSAT','CES','NPS')),
  score INTEGER NOT NULL,
  comment TEXT,
  created_at TEXT NOT NULL
);

-- Activos
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
  fiscal_life_years INTEGER,
  fiscal_method TEXT DEFAULT 'linea_recta',
  niif_life_years INTEGER,
  niif_method TEXT DEFAULT 'linea_recta',
  warranty_end TEXT,
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
CREATE TABLE IF NOT EXISTS asset_policies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  policy_number TEXT,
  insurer TEXT,
  start_date TEXT,
  end_date TEXT,
  coverage TEXT
);
CREATE TABLE IF NOT EXISTS asset_contracts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  vendor TEXT,
  contract_number TEXT,
  start_date TEXT,
  end_date TEXT,
  terms TEXT
);

-- CMDB y KB
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

-- Settings K/V
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);

-- Adjuntos
CREATE TABLE IF NOT EXISTS ticket_attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  file_name TEXT NOT NULL,
  file_path TEXT NOT NULL,
  uploaded_by INTEGER,
  uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  file_name TEXT NOT NULL,
  file_path TEXT NOT NULL,
  file_type TEXT,
  uploaded_by INTEGER,
  uploaded_at TEXT NOT NULL
);
"""

def _ensure_asset_files_extra_cols():
    cx = get_cx(); c = cx.cursor()
    try:
        c.execute("PRAGMA table_info(asset_files)")
        cols = {r[1] for r in c.fetchall()}
    except Exception:
        cols = set()
    add = []
    if "maintenance_id" not in cols: add.append(("maintenance_id","INTEGER"))
    if "contract_id" not in cols: add.append(("contract_id","INTEGER"))
    if "policy_id" not in cols: add.append(("policy_id","INTEGER"))
    for name, typ in add:
        try:
            c.execute(f"ALTER TABLE asset_files ADD COLUMN {name} {typ}")
        except Exception:
            pass
    cx.commit()

def migrate_schema():
    cx = get_cx()
    c = cx.cursor()
    for stmt in [s.strip() for s in INIT_SQL.split(";\n") if s.strip()]:
        try: c.execute(stmt + ";")
        except Exception: pass
    cx.commit()
    _ensure_asset_files_extra_cols()
    return cx

def run_query(sql, params=()):
    cx = migrate_schema()
    return pd.read_sql_query(sql, cx, params=params)

def run_script(sql, params=()):
    cx = migrate_schema()
    cx.execute(sql, params); cx.commit()

# Settings helpers
def get_setting(key: str, default: str|None=None):
    try:
        r = run_query("SELECT value FROM settings WHERE key=?", (key,))
        if r.empty: return default
        return r.loc[0,"value"]
    except Exception:
        return default

def set_setting(key: str, value: str):
    try:
        ex = run_query("SELECT COUNT(*) n FROM settings WHERE key=?", (key,))
        if int(ex.loc[0,"n"])>0:
            run_script("UPDATE settings SET value=? WHERE key=?", (value, key))
        else:
            run_script("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
        return True
    except Exception:
        return False

def get_upload_root():
    base = os.getenv("APP_UPLOAD_DIR")
    try:
        if base:
            os.makedirs(base, exist_ok=True)
            for sub in ("ticket_attachments","asset_files"):
                os.makedirs(os.path.join(base, sub), exist_ok=True)
            return base
        folder = os.path.join(os.path.dirname(APP_DB_PATH), "uploads")
        os.makedirs(folder, exist_ok=True)
        for sub in ("ticket_attachments","asset_files"):
            os.makedirs(os.path.join(folder, sub), exist_ok=True)
        return folder
    except Exception:
        return os.getcwd()

# --------- SSO token ---------
def try_token_sso():
    try:
        q = st.query_params
        user = q.get("user"); ts = q.get("ts"); sig = q.get("sig")
        if not (user and ts and sig and SSO_SHARED_SECRET): return
        check = hmac.new(SSO_SHARED_SECRET.encode(), f"{user}:{ts}".encode(), _hashlib.sha256).hexdigest()
        if check != sig: return
        df = run_query("SELECT * FROM users WHERE username=?", (user,))
        if df.empty:
            run_script("INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,datetime('now'))",
                       (user, hash_password("Temporal123!"), "usuario"))
            df = run_query("SELECT * FROM users WHERE username=?", (user,))
        st.session_state["auth_user"] = dict(df.iloc[0])
    except Exception:
        pass

# --------- SLA & Matriz ---------
def matrix_priority(service_id, urgency, impact):
    if not service_id: return "Media"
    r = run_query("SELECT priority FROM service_matrix WHERE service_id=? AND urgency=? AND impact=?", (int(service_id), urgency, impact))
    if r.empty: return "Media"
    return r.loc[0,"priority"]

def compute_sla(service_id, priority):
    if not service_id: return 8, 24
    r = run_query("SELECT response_hours, resolve_hours FROM service_sla WHERE service_id=? AND priority=?", (int(service_id), priority))
    if r.empty: return 8, 24
    return int(r.loc[0,"response_hours"]), int(r.loc[0,"resolve_hours"])

# --------- Depreciación ---------
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

def compute_depr_pair(row: dict):
    pm_f, acc_f, vl_f, m_f = compute_depreciation(float(row.get("acquisition_cost") or 0), float(row.get("salvage_value") or 0),
                                                  int(row.get("fiscal_life_years") or 0), row.get("acquisition_date") or "")
    pm_n, acc_n, vl_n, m_n = compute_depreciation(float(row.get("acquisition_cost") or 0), float(row.get("salvage_value") or 0),
                                                  int(row.get("niif_life_years") or 0), row.get("acquisition_date") or "")
    return (pm_f, acc_f, vl_f, m_f), (pm_n, acc_n, vl_n, m_n)

# --------- Sesión ---------
def get_current_user():
    return st.session_state.get("auth_user")

def login(username, password):
    df = run_query("SELECT * FROM users WHERE username=? AND active=1", (username,))
    if df.empty: return False
    row = df.iloc[0]
    if verify_password(password, row["password"]):
        st.session_state["auth_user"] = dict(row); return True
    return False

def ensure_admin_exists():
    df = run_query("SELECT COUNT(*) n FROM users")
    if int(df.loc[0,"n"]) == 0:
        pwd = "Admin1234!"
        run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                   ("admin","admin@example.com",hash_password(pwd),"admin"))
        st.info(f"Se creó el usuario admin / {pwd}. Cambia la contraseña.")

# --------- UI ---------
def sidebar_menu():
    user = get_current_user()
    if user:
        st.sidebar.markdown(f"**Conectado:** `{user['username']}` ({user['role']})")
        if st.sidebar.button("Cerrar sesión", key="btn_logout"):
            st.session_state.pop("auth_user", None); st.rerun()
    else:
        st.sidebar.info("No has iniciado sesión.")
    return st.sidebar.selectbox("Ir a:", ["Dashboard","Tickets – Nuevo","Tickets – Bandeja","Ticket – Detalle","Activos","CMDB","Mi Perfil y Seguridad","Configuración"])

def page_login():
    st.title("Mesa de Ayuda + Inventarios — ITIL 4 (Enterprise+)")
    ensure_admin_exists()
    try_token_sso()
    tab_login, tab_reg, tab_reset = st.tabs(["Iniciar sesión","Registrarse","Recuperar"])
    with tab_login:
        u = st.text_input("Usuario", key="login_user")
        p = st.text_input("Contraseña", type="password", key="login_pwd")
        if st.button("Entrar", key="btn_login"):
            if login(u,p):
                st.success("Bienvenido."); st.rerun()
            else:
                st.error("Credenciales inválidas.")
    with tab_reg:
        u = st.text_input("Usuario nuevo", key="reg_user")
        e = st.text_input("Email", key="reg_email")
        p1 = st.text_input("Contraseña", type="password", key="reg_pwd1")
        p2 = st.text_input("Confirmar", type="password", key="reg_pwd2")
        if st.button("Crear cuenta", key="btn_register"):
            if not u or not e or not p1 or p1!=p2 or len(p1)<8:
                st.error("Completa todos los campos y usa 8+ caracteres.")
            else:
                run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                           (u,e,hash_password(p1),"usuario"))
                st.success("Usuario creado. Ya puedes iniciar sesión.")
    with tab_reset:
        e = st.text_input("Tu email", key="reset_email")
        if st.button("Enviar enlace", key="btn_reset_send"):
            st.info("Demo: envía un token de recuperación si SMTP está configurado.")

def _ticket_code():
    today_str = datetime.utcnow().strftime("%Y%m%d")
    df = run_query("SELECT COUNT(*) n FROM tickets WHERE substr(created_at,1,10)=substr(datetime('now'),1,10)")
    seq = int(df.loc[0,"n"]) + 1
    return f"TCK-{today_str}-{seq:04d}"

def page_tickets_nuevo():
    st.header("Nuevo ticket")
    user = get_current_user()
    if not user: st.warning("Inicia sesión."); return
    col1, col2 = st.columns(2)
    with col1:
        title = st.text_input("Título")
        desc = st.text_area("Descripción")
        itil_type = st.selectbox("Tipo ITIL", ["Incidente","Solicitud","Cambio","Problema"])
        category = st.selectbox("Categoría (reportes)", ["Incidente","Solicitud","Ajuste","Consulta"])
        watchers = st.text_input("Watchers (emails separados por coma)", key="new_watchers")
        files = st.file_uploader("Adjuntos (opcional)", type=None, accept_multiple_files=True, key="new_ticket_files")
    with col2:
        services = run_query("SELECT id, name FROM services ORDER BY name")
        if services.empty:
            st.info("No hay servicios cargados. Crea algunos en Configuración → Servicios/SLAs.")
            service_id = None
        else:
            service_name = st.selectbox("Servicio", services["name"])
            service_id = int(services[services["name"]==service_name]["id"].iloc[0])
        urgency = st.selectbox("Urgencia", ["Baja","Media","Alta"])
        impact = st.selectbox("Impacto", ["Bajo","Medio","Alto"])
        priority = matrix_priority(service_id, urgency, impact) if service_id else "Media"
        st.markdown(f"**Prioridad sugerida:** `{priority}`")

    if st.button("Crear", key="btn_create_ticket", type="primary"):
        if not title or not service_id:
            st.error("Completa título y servicio."); return
        code_t = _ticket_code()
        now = datetime.utcnow().isoformat()
        resp_h, res_h = compute_sla(service_id, priority)
        response_due = (datetime.utcnow() + timedelta(hours=resp_h)).isoformat()
        resolve_due = (datetime.utcnow() + timedelta(hours=res_h)).isoformat()
        run_script("""INSERT INTO tickets(code,title,description,category,itil_type,service_id,priority,urgency,impact,status,created_by,watchers_emails,created_at,updated_at,response_due_at,due_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (code_t,title.strip(),desc.strip(),category,itil_type,int(service_id),priority,urgency,impact,"Nuevo",int(user["id"]),watchers.strip(),now,now,response_due,resolve_due))
        # Adjuntos
        if files:
            up = get_upload_root()
            tid = int(run_query("SELECT id FROM tickets WHERE code=?", (code_t,)).loc[0,"id"])
            for f in files:
                safe = safe_filename(f.name)
                dest = os.path.join(up, "ticket_attachments", f"{code_t}_{safe}")
                with open(dest, "wb") as out: out.write(f.read())
                run_script("INSERT INTO ticket_attachments(ticket_id,file_name,file_path,uploaded_by,uploaded_at) VALUES(?,?,?,?,datetime('now'))",
                           (tid, safe, dest, int(user["id"])))
        notify_webhooks("ticket_created", {"code": code_t, "title": title, "type": itil_type, "priority": priority})
        st.success(f"Creado: {code_t}")
        st.rerun()

def page_tickets_bandeja():
    st.header("Bandeja de tickets")
    user = get_current_user()
    if not user: st.warning("Inicia sesión."); return
    t1, t2 = st.columns([3,1])
    with t1:
        tab_inc, tab_sol, tab_cam, tab_prob = st.tabs(["Incidentes","Solicitudes","Cambios","Problemas"])
    with t2:
        teams = run_query("SELECT id, name FROM teams ORDER BY name")
        team_filter = st.selectbox("Equipo", ["Todos"] + ([] if teams.empty else list(teams["name"])))
    def _grid(itil):
        base = """SELECT t.id, t.code, t.title, s.name as servicio, t.priority, t.status, u.username AS owner, t.updated_at
                  FROM tickets t
                  LEFT JOIN services s ON s.id=t.service_id
                  JOIN users u ON u.id=t.created_by
                  LEFT JOIN users ag ON ag.id=t.assigned_to
                  WHERE t.itil_type=?"""
        params = [itil]
        if user["role"]=="usuario":
            base += " AND t.created_by=?"; params.append(int(user["id"]))
        if team_filter and team_filter!="Todos":
            base += " AND ag.team_id=(SELECT id FROM teams WHERE name=?)"; params.append(team_filter)
        df = run_query(base + " ORDER BY t.updated_at DESC", tuple(params))
        st.dataframe(df, use_container_width=True)
        c1,c2,c3 = st.columns(3)
        tid = c1.text_input(f"ID a abrir ({itil})", "", key=f"open_{itil}")
        if c2.button("Abrir", key=f"btn_open_{itil}") and tid.strip().isdigit():
            st.session_state["current_ticket_id"] = int(tid.strip())
            st.rerun()
    with tab_inc: _grid("Incidente")
    with tab_sol: _grid("Solicitud")
    with tab_cam: _grid("Cambio")
    with tab_prob: _grid("Problema")

def render_inline_view(file_path: str):
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in [".png",".jpg",".jpeg",".gif",".webp"]:
            with open(file_path, "rb") as fh:
                st.image(fh.read(), caption=os.path.basename(file_path), use_container_width=True)
        elif ext == ".pdf":
            with open(file_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            html = f'<iframe src="data:application/pdf;base64,{b64}" width="100%%" height="600px"></iframe>'
            components.html(html, height=620, scrolling=True)
        else:
            st.info(f"Vista previa no soportada para: {os.path.basename(file_path)}")
    except Exception as e:
        st.warning(f"No se pudo mostrar el archivo: {e}")

def page_ticket_detalle():
    user = get_current_user()
    if not user: st.warning("Inicia sesión."); return
    tid = st.session_state.get("current_ticket_id")
    if not tid:
        st.info("Selecciona un ticket desde la bandeja."); return
    t = run_query("""SELECT t.*, s.name as service_name, u.username as owner_name, u.email as owner_email,
                            ag.username as agent_name, ag.email as agent_email
                     FROM tickets t
                     LEFT JOIN services s ON s.id=t.service_id
                     JOIN users u ON u.id=t.created_by
                     LEFT JOIN users ag ON ag.id=t.assigned_to
                     WHERE t.id=?""", (int(tid),))
    if t.empty: st.error("No encontrado."); return
    row = t.iloc[0]
    st.header(f"[{row['code']}] {row['title']}")
    st.caption(f"Propietario: {row['owner_name']} · Servicio: {row['service_name']} · Estado: {row['status']} · Prioridad: {row['priority']}")

    if user["role"] in ("admin","agente"):
        c1,c2,c3,c4 = st.columns(4)
        agentes = run_query("SELECT id, username FROM users WHERE role='agente' AND active=1 ORDER BY username")
        assignee = c1.selectbox("Asignar a", [] if agentes.empty else list(agentes["username"]), key="assign_user")
        if c1.button("Asignar", key="btn_assign"):
            if not agentes.empty:
                uid = int(agentes[agentes["username"]==assignee]["id"].iloc[0])
                run_script("UPDATE tickets SET assigned_to=?, updated_at=datetime('now') WHERE id=?", (uid, int(tid)))
                aginfo = run_query("SELECT email FROM users WHERE id=?", (uid,))
                ag_email = None if aginfo.empty else aginfo.loc[0,'email']
                notify_webhooks("ticket_assigned", {"code": row["code"], "assigned_to": assignee})
                if row.get('owner_email'):
                    send_email(row['owner_email'], f"[{row['code']}] Ticket asignado", f"Tu ticket fue asignado a: {assignee}.")
                if ag_email:
                    send_email(ag_email, f"[{row['code']}] Se te ha asignado un ticket", f"Se te asignó el ticket {row['code']} - {row['title']}.")
                st.success(f"Asignado a {assignee}."); st.rerun()
        new_status = c2.selectbox("Nuevo estado", ["Nuevo","Asignado","En Progreso","En Espera","Aprobación","Resuelto","Cerrado"], key="new_status")
        if c2.button("Aplicar estado", key="btn_state"):
            run_script("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE id=?", (new_status, int(tid)))
            run_script("INSERT INTO ticket_status_history(ticket_id,status,changed_by,changed_at) VALUES(?,?,?,datetime('now'))", (int(tid), new_status, int(user["id"])))
            notify_webhooks("ticket_status", {"code": row["code"], "status": new_status})
            recips = []
            if row.get('owner_email'): recips.append(row['owner_email'])
            if row.get('watchers_emails'):
                for rcp in str(row['watchers_emails']).replace(';',',').split(','):
                    rcp = rcp.strip(); 
                    if rcp: recips.append(rcp)
            for to in recips:
                try:
                    send_email(to, f"[{row['code']}] Estado actualizado: {new_status}", f"Tu ticket {row['code']} cambió a: {new_status}.")
                except Exception:
                    pass
            if new_status=="Cerrado" and row.get('owner_email'):
                send_email(row['owner_email'], f"[{row['code']}] Encuesta de satisfacción", "Gracias por usar la mesa de ayuda. Por favor califica el servicio desde tu portal.")
            st.success("Estado actualizado."); st.rerun()

    st.subheader("Descripción")
    st.write(row["description"] or "")

    if row["itil_type"]=="Cambio":
        st.subheader("Aprobaciones")
        flow = run_query("""SELECT l.level, a.name as area
                            FROM service_area_levels l JOIN areas a ON a.id=l.area_id
                            WHERE l.service_id=? ORDER BY l.level""", (int(row["service_id"]),))
        if flow.empty:
            st.info("No hay flujo de aprobaciones configurado para este servicio.")
        else:
            pending = run_query("""SELECT ca.*, a.name as area_name, u.username as approver
                                   FROM change_approvals ca
                                   JOIN areas a ON a.id=ca.area_id
                                   LEFT JOIN users u ON u.id=ca.approver_user_id
                                   WHERE ca.ticket_id=? ORDER BY ca.level""", (int(tid),))
            if pending.empty:
                for i, r in flow.iterrows():
                    aid = int(run_query("SELECT id FROM areas WHERE name=?", (r["area"],)).loc[0,"id"])
                    run_script("INSERT INTO change_approvals(ticket_id,service_id,area_id,level,status) VALUES(?,?,?,?,?)",
                               (int(tid), int(row["service_id"]), aid, int(r["level"]), "Pendiente"))
                pending = run_query("""SELECT ca.*, a.name as area_name FROM change_approvals ca JOIN areas a ON a.id=ca.area_id WHERE ca.ticket_id=? ORDER BY ca.level""", (int(tid),))
            st.dataframe(pending[["level","area_name","status","approver_user_id","decided_at","notes"]], use_container_width=True)
            if user["role"] in ("admin","agente"):
                lvl = st.number_input("Nivel a decidir", min_value=1, step=1, key="appr_lvl")
                decision = st.selectbox("Decisión", ["Aprobar","Rechazar"], key="appr_dec")
                notes = st.text_input("Notas", key="appr_notes")
                if st.button("Registrar decisión", key="btn_appr"):
                    stime = datetime.utcnow().isoformat()
                    new_status = "Aprobado" if decision=="Aprobar" else "Rechazado"
                    run_script("UPDATE change_approvals SET status=?, approver_user_id=?, decided_at=?, notes=? WHERE ticket_id=? AND level=?",
                               (new_status, int(user["id"]), stime, notes, int(tid), int(lvl)))
                    rest = run_query("SELECT COUNT(*) n FROM change_approvals WHERE ticket_id=? AND status='Pendiente'", (int(tid),))
                    if int(rest.loc[0,"n"])==0:
                        run_script("UPDATE tickets SET status='En Progreso', updated_at=datetime('now') WHERE id=?", (int(tid),))
                        notify_webhooks("change_approved", {"code": row["code"]})
                    st.success("Decisión registrada."); st.rerun()

    st.subheader("Adjuntos")
    at = run_query("SELECT id, file_name, file_path, uploaded_at FROM ticket_attachments WHERE ticket_id=? ORDER BY uploaded_at DESC", (int(tid),))
    if not at.empty:
        for i0, r0 in at.iterrows():
            try:
                with open(r0["file_path"], "rb") as fh:
                    st.download_button(label=f"Descargar: {r0['file_name']}", data=fh.read(), file_name=r0["file_name"], key=f"dl_att_{r0['id']}")
                render_inline_view(r0["file_path"])
            except Exception:
                st.write(f"No se encuentra: {r0['file_name']}")
    upfiles = st.file_uploader("Agregar adjuntos", accept_multiple_files=True, key="att_more")
    if upfiles:
        up = get_upload_root()
        for f in upfiles:
            safe = safe_filename(f.name)
            dest = os.path.join(up, "ticket_attachments", f"{row['code']}_{safe}")
            with open(dest, "wb") as out: out.write(f.read())
            run_script("INSERT INTO ticket_attachments(ticket_id,file_name,file_path,uploaded_by,uploaded_at) VALUES(?,?,?,?,datetime('now'))", (int(tid), safe, dest, int(user["id"])))
        st.success("Adjuntos agregados."); st.rerun()

    st.subheader("Historial de estados")
    h = run_query("""SELECT h.status, h.changed_at, u.username AS by_user
                     FROM ticket_status_history h JOIN users u ON u.id=h.changed_by
                     WHERE h.ticket_id=? ORDER BY h.changed_at DESC""", (int(tid),))
    st.dataframe(h, use_container_width=True)

    st.subheader("Encuesta (propietario)")
    if user["id"] == row["created_by"] and row["status"] in ("Resuelto","Cerrado"):
        c1,c2,c3 = st.columns(3)
        csat = c1.slider("CSAT (1–5)", 1, 5, 5, key="csat_slider")
        ces = c2.slider("CES (1–7)", 1, 7, 3, key="ces_slider")
        nps = c3.slider("NPS (0–10)", 0, 10, 10, key="nps_slider")
        comment = st.text_input("Comentario", key="survey_comment")
        if st.button("Enviar encuesta", key="btn_survey", type="primary"):
            now = datetime.utcnow().isoformat()
            for ttype, score in [("CSAT", int(csat)), ("CES", int(ces)), ("NPS", int(nps))]:
                run_script("INSERT INTO ticket_surveys(ticket_id,survey_type,score,comment,created_at) VALUES(?,?,?,?,?)",
                           (int(tid), ttype, score, comment, now))
            st.success("¡Gracias por tu retroalimentación!")

def page_activos():
    st.header("Activos")
    with st.form("new_asset"):
        c1, c2, c3 = st.columns(3)
        with c1: code = st.text_input("Código")
        with c2: name = st.text_input("Nombre")
        with c3: category = st.text_input("Categoría")
        c1,c2,c3,c4 = st.columns(4)
        with c1: serial = st.text_input("Serial")
        with c2: acq_date = st.date_input("Fecha adquisición", value=date.today())
        with c3: cost = st.number_input("Costo", min_value=0.0, step=0.01)
        with c4: salvage = st.number_input("Valor residual", min_value=0.0, step=0.01)
        c1,c2,c3 = st.columns(3)
        with c1: fiscal_life = st.number_input("Vida útil (años) Fiscal", min_value=0, step=1)
        with c2: niif_life = st.number_input("Vida útil (años) NIIF", min_value=0, step=1)
        with c3: warranty_end = st.date_input("Fin de garantía", value=date.today())
        submitted = st.form_submit_button("Guardar activo", use_container_width=True)
    if submitted and name.strip():
        acq_str = acq_date.isoformat() if acq_date else None
        war_str = warranty_end.isoformat() if warranty_end else None
        ex = run_query("SELECT id FROM assets WHERE code=?", (code,))
        if ex.empty:
            run_script("""INSERT INTO assets(code,name,category,serial,acquisition_cost,acquisition_date,salvage_value,
                                             fiscal_life_years, niif_life_years, warranty_end)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (code,name,category,serial,float(cost),acq_str,float(salvage),int(fiscal_life),int(niif_life),war_str))
            st.success("Activo creado.")
        else:
            run_script("""UPDATE assets SET name=?, category=?, serial=?, acquisition_cost=?, acquisition_date=?, salvage_value=?, 
                                         fiscal_life_years=?, niif_life_years=?, warranty_end=? WHERE code=?""",
                       (name,category,serial,float(cost),acq_str,float(salvage),int(fiscal_life),int(niif_life),war_str,code))
            st.success("Activo actualizado.")

    a = run_query("SELECT id, code, name, category, acquisition_cost, acquisition_date, salvage_value, fiscal_life_years, niif_life_years, warranty_end FROM assets ORDER BY name")
    st.dataframe(a, use_container_width=True)

    st.subheader("Detalle / Hoja de Vida / Depreciación")
    if not a.empty:
        sel = st.selectbox("Selecciona activo", a["code"], key="asset_sel")
        r = run_query("SELECT * FROM assets WHERE code=?", (sel,)).iloc[0].to_dict()
        t1,t2,t3,t4 = st.tabs(["Detalle","Hoja de Vida","Pólizas/Contratos","Depreciación (Fiscal/NIIF)"])
        with t1:
            st.json(r)
        with t2:
            st.write("Asignaciones")
            asg = run_query("SELECT * FROM asset_assignments WHERE asset_id=? ORDER BY assigned_at DESC", (int(r["id"]),))
            st.dataframe(asg, use_container_width=True)
            with st.form("form_asg"):
                loc = st.text_input("Ubicación/Área", key="asg_loc")
                notes = st.text_input("Notas", key="asg_notes")
                if st.form_submit_button("Registrar asignación", use_container_width=True):
                    run_script("INSERT INTO asset_assignments(asset_id,location,assigned_at,notes) VALUES(?,?,datetime('now'),?)",
                               (int(r["id"]), loc, notes))
                    st.success("Asignación registrada."); st.rerun()
            st.write("Mantenimientos")
            mt = run_query("SELECT * FROM asset_maintenances WHERE asset_id=? ORDER BY performed_at DESC", (int(r["id"]),))
            st.dataframe(mt, use_container_width=True)
            with st.form("form_maint"):
                mtype = st.text_input("Tipo", key="mt_type")
                mdesc = st.text_area("Descripción", key="mt_desc")
                mcost = st.number_input("Costo", min_value=0.0, step=0.01, key="mt_cost")
                if st.form_submit_button("Registrar mantenimiento", use_container_width=True):
                    run_script("INSERT INTO asset_maintenances(asset_id,maintenance_type,description,cost,performed_at) VALUES(?,?,?,?,datetime('now'))",
                               (int(r["id"]), mtype, mdesc, float(mcost)))
                    st.success("Mantenimiento registrado."); st.rerun()

            st.markdown("#### Adjuntos por mantenimiento")
            mt2 = run_query("SELECT id, maintenance_type, performed_at FROM asset_maintenances WHERE asset_id=? ORDER BY performed_at DESC", (int(r["id"]),))
            if not mt2.empty:
                sel_mt = st.selectbox("Selecciona mantenimiento", mt2.apply(lambda x: f"{x['id']} – {x['maintenance_type']} – {x['performed_at']}", axis=1), key="sel_mt_att")
                sel_id = int(sel_mt.split(" – ")[0])
                af = run_query("SELECT id, file_name, file_path, uploaded_at FROM asset_files WHERE asset_id=? AND maintenance_id=? ORDER BY uploaded_at DESC", (int(r["id"]), sel_id))
                if not af.empty:
                    for _, r2 in af.iterrows():
                        try:
                            with open(r2["file_path"], "rb") as fh:
                                st.download_button(label=f"Descargar: {r2['file_name']}", data=fh.read(), file_name=r2["file_name"], key=f"dl_mt_{r2['id']}")
                            render_inline_view(r2["file_path"])
                        except Exception:
                            st.write(f"No se encuentra: {r2['file_name']}")
                up_mt = st.file_uploader("Subir adjuntos de mantenimiento", accept_multiple_files=True, key="up_mt_files")
                if up_mt:
                    up = get_upload_root()
                    folder = os.path.join(up, "asset_files")
                    os.makedirs(folder, exist_ok=True)
                    for f in up_mt:
                        safe = safe_filename(f.name)
                        dest = os.path.join(folder, f"MT{sel_id}_{safe}")
                        with open(dest, "wb") as out: out.write(f.read())
                        run_script("INSERT INTO asset_files(asset_id,file_name,file_path,file_type,maintenance_id,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                                   (int(r["id"]), safe, dest, "mantenimiento", sel_id, None))
                    st.success("Adjuntos agregados."); st.rerun()
            else:
                st.info("Aún no hay mantenimientos para adjuntar archivos.")

        with t3:
            st.write("Pólizas")
            pol = run_query("SELECT * FROM asset_policies WHERE asset_id=? ORDER BY end_date DESC", (int(r["id"]),))
            st.dataframe(pol, use_container_width=True)
            with st.form("form_pol"):
                pn = st.text_input("Número póliza", key="pol_num")
                ins = st.text_input("Aseguradora", key="pol_ins")
                sd = st.date_input("Inicio", key="pol_start")
                ed = st.date_input("Fin", key="pol_end")
                cov = st.text_input("Cobertura", key="pol_cov")
                if st.form_submit_button("Agregar póliza", use_container_width=True):
                    run_script("INSERT INTO asset_policies(asset_id,policy_number,insurer,start_date,end_date,coverage) VALUES(?,?,?,?,?,?)",
                               (int(r["id"]), pn, ins, sd.isoformat(), ed.isoformat(), cov))
                    st.success("Póliza agregada."); st.rerun()

            st.markdown("#### Adjuntos por póliza")
            pol2 = run_query("SELECT id, policy_number, insurer, end_date FROM asset_policies WHERE asset_id=? ORDER BY end_date DESC", (int(r["id"]),))
            if not pol2.empty:
                sel_pol = st.selectbox("Selecciona póliza", pol2.apply(lambda x: f"{x['id']} – {x['policy_number']} – {x['insurer']}", axis=1), key="sel_pol_att")
                pol_id = int(sel_pol.split(" – ")[0])
                afp = run_query("SELECT id, file_name, file_path, uploaded_at FROM asset_files WHERE asset_id=? AND policy_id=? ORDER BY uploaded_at DESC", (int(r["id"]), pol_id))
                if not afp.empty:
                    for _, r3 in afp.iterrows():
                        try:
                            with open(r3["file_path"], "rb") as fh:
                                st.download_button(label=f"Descargar: {r3['file_name']}", data=fh.read(), file_name=r3["file_name"], key=f"dl_pol_{r3['id']}")
                            render_inline_view(r3["file_path"])
                        except Exception:
                            st.write(f"No se encuentra: {r3['file_name']}")
                up_pol = st.file_uploader("Subir adjuntos de póliza", accept_multiple_files=True, key="up_pol_files")
                if up_pol:
                    up = get_upload_root()
                    folder = os.path.join(up, "asset_files"); os.makedirs(folder, exist_ok=True)
                    for f in up_pol:
                        safe = safe_filename(f.name)
                        dest = os.path.join(folder, f"POL{pol_id}_{safe}")
                        with open(dest, "wb") as out: out.write(f.read())
                        run_script("INSERT INTO asset_files(asset_id,file_name,file_path,file_type,policy_id,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                                   (int(r["id"]), safe, dest, "poliza", pol_id, None))
                    st.success("Adjuntos agregados."); st.rerun()
            else:
                st.info("Aún no hay pólizas para adjuntar archivos.")

            st.write("Contratos")
            con = run_query("SELECT * FROM asset_contracts WHERE asset_id=? ORDER BY end_date DESC", (int(r["id"]),))
            st.dataframe(con, use_container_width=True)
            with st.form("form_con"):
                ven = st.text_input("Proveedor", key="con_vendor")
                cn = st.text_input("N° contrato", key="con_num")
                sd2 = st.date_input("Inicio", key="con_start")
                ed2 = st.date_input("Fin", key="con_end")
                terms = st.text_area("Términos", key="con_terms")
                if st.form_submit_button("Agregar contrato", use_container_width=True):
                    run_script("INSERT INTO asset_contracts(asset_id,vendor,contract_number,start_date,end_date,terms) VALUES(?,?,?,?,?,?)",
                               (int(r["id"]), ven, cn, sd2.isoformat(), ed2.isoformat(), terms))
                    st.success("Contrato agregado."); st.rerun()

            st.markdown("#### Adjuntos por contrato")
            con2 = run_query("SELECT id, contract_number, vendor, end_date FROM asset_contracts WHERE asset_id=? ORDER BY end_date DESC", (int(r["id"]),))
            if not con2.empty:
                sel_con = st.selectbox("Selecciona contrato", con2.apply(lambda x: f"{x['id']} – {x['contract_number']} – {x['vendor']}", axis=1), key="sel_con_att")
                con_id = int(sel_con.split(" – ")[0])
                afc = run_query("SELECT id, file_name, file_path, uploaded_at FROM asset_files WHERE asset_id=? AND contract_id=? ORDER BY uploaded_at DESC", (int(r["id"]), con_id))
                if not afc.empty:
                    for _, r4 in afc.iterrows():
                        try:
                            with open(r4["file_path"], "rb") as fh:
                                st.download_button(label=f"Descargar: {r4['file_name']}", data=fh.read(), file_name=r4["file_name"], key=f"dl_con_{r4['id']}")
                            render_inline_view(r4["file_path"])
                        except Exception:
                            st.write(f"No se encuentra: {r4['file_name']}")
                up_con = st.file_uploader("Subir adjuntos de contrato", accept_multiple_files=True, key="up_con_files")
                if up_con:
                    up = get_upload_root()
                    folder = os.path.join(up, "asset_files"); os.makedirs(folder, exist_ok=True)
                    for f in up_con:
                        safe = safe_filename(f.name)
                        dest = os.path.join(folder, f"CON{con_id}_{safe}")
                        with open(dest, "wb") as out: out.write(f.read())
                        run_script("INSERT INTO asset_files(asset_id,file_name,file_path,file_type,contract_id,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                                   (int(r["id"]), safe, dest, "contrato", con_id, None))
                    st.success("Adjuntos agregados."); st.rerun()
            else:
                st.info("Aún no hay contratos para adjuntar archivos.")

        with t4:
            st.subheader("Adjuntos del activo (generales)")
            af = run_query("SELECT id, file_name, file_path, uploaded_at, file_type FROM asset_files WHERE asset_id=? AND maintenance_id IS NULL AND policy_id IS NULL AND contract_id IS NULL ORDER BY uploaded_at DESC", (int(r["id"]),))
            if not af.empty:
                for _, r0 in af.iterrows():
                    try:
                        with open(r0["file_path"], "rb") as fh:
                            st.download_button(label=f"Descargar: {r0['file_name']} ({r0.get('file_type','')})", data=fh.read(), file_name=r0["file_name"], key=f"dl_af_{r0['id']}")
                        render_inline_view(r0["file_path"])
                    except Exception:
                        st.write(f"No se encuentra: {r0['file_name']}")
            new_af = st.file_uploader("Subir adjuntos generales", accept_multiple_files=True, key="af_upload")
            af_type = st.text_input("Tipo (general, factura, garantia, foto, etc.)", key="af_type")
            if new_af:
                up = get_upload_root()
                folder = os.path.join(up, "asset_files"); os.makedirs(folder, exist_ok=True)
                for f in new_af:
                    safe = safe_filename(f.name)
                    dest = os.path.join(folder, f"{r['code']}_{safe}")
                    with open(dest, "wb") as out: out.write(f.read())
                    run_script("INSERT INTO asset_files(asset_id,file_name,file_path,file_type,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,datetime('now'))", (int(r["id"]), safe, dest, af_type or "general", None))
                st.success("Adjuntos agregados."); st.rerun()

            (pm_f, acc_f, vl_f, m_f), (pm_n, acc_n, vl_n, m_n) = compute_depr_pair(r)
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Mensual Fiscal", pm_f); c2.metric("Acum. Fiscal", acc_f); c3.metric("Libros Fiscal", vl_f); c4.metric("Meses Fisc.", m_f)
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Mensual NIIF", pm_n); c2.metric("Acum. NIIF", acc_n); c3.metric("Libros NIIF", vl_n); c4.metric("Meses NIIF", m_n)
            if st.button("Exportar XLSX", key="btn_xlsx", type="primary"):
                df_pol = run_query("SELECT * FROM asset_policies WHERE asset_id=?", (int(r["id"]),))
                df_con = run_query("SELECT * FROM asset_contracts WHERE asset_id=?", (int(r["id"]),))
                df_asg = run_query("SELECT * FROM asset_assignments WHERE asset_id=?", (int(r["id"]),))
                df_mt = run_query("SELECT * FROM asset_maintenances WHERE asset_id=?", (int(r["id"]),))
                bio = io.BytesIO()
                with pd.ExcelWriter(bio, engine="openpyxl") as writer:
                    pd.DataFrame([r]).to_excel(writer, index=False, sheet_name="Activo")
                    df_pol.to_excel(writer, index=False, sheet_name="Polizas")
                    df_con.to_excel(writer, index=False, sheet_name="Contratos")
                    df_asg.to_excel(writer, index=False, sheet_name="Asignaciones")
                    df_mt.to_excel(writer, index=False, sheet_name="Mantenimientos")
                st.download_button("Descargar XLSX", data=bio.getvalue(), file_name=f"activo_{r['code']}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def page_cmdb():
    st.header("CMDB")
    with st.form("ci_form"):
        name = st.text_input("Nombre CI", key="ci_name")
        ci_type = st.text_input("Tipo CI", key="ci_type")
        if st.form_submit_button("Crear CI", use_container_width=True):
            run_script("INSERT INTO ci_items(name,ci_type) VALUES(?,?)", (name,ci_type))
            st.success("CI creado."); st.rerun()
    cis = run_query("SELECT * FROM ci_items ORDER BY name")
    st.dataframe(cis, use_container_width=True)

    st.subheader("Relaciones CI")
    cis2 = run_query("SELECT id, name FROM ci_items ORDER BY name")
    if not cis2.empty:
        with st.form("ci_rel_form"):
            p = st.selectbox("CI Padre", cis2['name'], key="rel_parent")
            c = st.selectbox("CI Hijo", cis2['name'], key="rel_child")
            r = st.text_input("Tipo de relación", key="rel_type")
            if st.form_submit_button("Crear relación", use_container_width=True):
                pid = int(cis2.loc[cis2['name']==p,'id'].iloc[0])
                cid = int(cis2.loc[cis2['name']==c,'id'].iloc[0])
                if pid == cid:
                    st.error("Padre y Hijo no pueden ser el mismo.")
                else:
                    run_script("INSERT INTO ci_relations(parent_ci_id,child_ci_id,relation_type) VALUES(?,?,?)", (pid,cid,r.strip()))
                    st.success("Relación creada."); st.rerun()
    rel = run_query("""SELECT pr.name AS padre, ch.name AS hijo, r.relation_type
                       FROM ci_relations r
                       JOIN ci_items pr ON pr.id=r.parent_ci_id
                       JOIN ci_items ch ON ch.id=r.child_ci_id
                       ORDER BY pr.name, ch.name""")
    st.dataframe(rel, use_container_width=True)

def page_mi_perfil_seguridad():
    st.header("Mi Perfil y Seguridad")
    user = get_current_user()
    if user is None:
        st.warning("Inicia sesión para gestionar tu perfil."); return

    st.subheader("Cambiar contraseña")
    with st.form("form_change_pwd"):
        old = st.text_input("Contraseña actual", type="password", key="chg_pwd_old")
        new1 = st.text_input("Nueva contraseña", type="password", key="chg_pwd_new1")
        new2 = st.text_input("Confirmar nueva contraseña", type="password", key="chg_pwd_new2")
        submitted = st.form_submit_button("Actualizar contraseña", use_container_width=True)
    if submitted:
        u = run_query("SELECT id, password FROM users WHERE id=?", (user['id'],))
        if u.empty: st.error("Usuario no encontrado.")
        elif not verify_password(old, u.loc[0,'password']): st.error("La contraseña actual no es correcta.")
        elif len(new1) < 8 or new1 != new2: st.error("La nueva contraseña debe tener al menos 8 caracteres y coincidir.")
        else:
            run_script("UPDATE users SET password=? WHERE id=?", (hash_password(new1), user['id']))
            st.success("Contraseña actualizada.")

    st.divider()
    st.subheader("Cambiar correo asociado")
    with st.form("form_change_email"):
        current_email = st.text_input("Correo actual", value=user.get("email") or "", disabled=True, key="chg_mail_current")
        new_email = st.text_input("Nuevo correo", key="chg_mail_new")
        confirm_pwd = st.text_input("Confirma tu contraseña", type="password", key="chg_mail_pwd")
        submitted_e = st.form_submit_button("Actualizar correo", use_container_width=True)
    if submitted_e:
        if not new_email: st.error("Ingresa el nuevo correo.")
        else:
            u = run_query("SELECT id, password, email FROM users WHERE id=?", (user['id'],))
            if u.empty: st.error("Usuario no encontrado.")
            elif not verify_password(confirm_pwd, u.loc[0,'password']): st.error("La contraseña no es correcta.")
            elif int(run_query("SELECT COUNT(*) n FROM users WHERE email=? AND id<>?", (new_email, int(user['id']))).loc[0,'n'])>0:
                st.error("Ese correo ya está en uso por otro usuario.")
            else:
                run_script("UPDATE users SET email=? WHERE id=?", (new_email, user['id']))
                st.success("Correo actualizado.")
                df = run_query("SELECT * FROM users WHERE id=?", (user['id'],))
                st.session_state["auth_user"] = dict(df.iloc[0])

def page_configuracion():
    st.header("Configuración")
    user = get_current_user()
    if user is None or user["role"]!="admin":
        st.info("Solo administradores pueden modificar configuración."); return
    tabs = st.tabs(["Usuarios/Roles","Servicios/SLAs","Matriz U×I","Aprobaciones","Notificaciones/SSO"])
    with tabs[0]:
        u = run_query("SELECT id, username, email, role, team_id, active FROM users ORDER BY id")
        st.dataframe(u, use_container_width=True)
        st.markdown("### Crear usuario")
        with st.form("create_user"):
            ux = st.text_input("Usuario", key="cfg_user")
            ex = st.text_input("Email", key="cfg_email")
            rx = st.selectbox("Rol", ["admin","agente","usuario"], key="cfg_role")
            px = st.text_input("Contraseña", type="password", key="cfg_pwd")
            if st.form_submit_button("Crear", use_container_width=True):
                run_script("INSERT INTO users(username,email,password,role,created_at) VALUES(?,?,?,?,datetime('now'))",
                           (ux,ex,hash_password(px),rx))
                st.success("Usuario creado."); st.rerun()
        st.markdown("### Equipos")
        tname = st.text_input("Nuevo equipo", key="cfg_team")
        if st.button("Crear equipo", key="btn_team_create"):
            run_script("INSERT INTO teams(name) VALUES(?)", (tname,)); st.success("Equipo creado."); st.rerun()
        st.markdown("### Editar usuario")
        u2 = run_query("SELECT id, username, email, role, active FROM users ORDER BY username")
        if not u2.empty:
            sel_u = st.selectbox("Usuario", u2["username"], key="cfg_edit_user_sel")
            row = u2[u2["username"]==sel_u].iloc[0]
            with st.form("cfg_edit_user_form"):
                new_email = st.text_input("Nuevo email", value=row["email"] or "", key="cfg_edit_user_email")
                new_role = st.selectbox("Rol", ["admin","agente","usuario"], index=["admin","agente","usuario"].index(row["role"]), key="cfg_edit_user_role")
                new_active = st.checkbox("Activo", value=bool(row["active"]), key="cfg_edit_user_active")
                new_pwd = st.text_input("Resetear contraseña (opcional)", type="password", key="cfg_edit_user_pwd")
                ok = st.form_submit_button("Guardar cambios", use_container_width=True)
            if ok:
                if new_email and int(run_query("SELECT COUNT(*) n FROM users WHERE email=? AND id <> ?", (new_email, int(row["id"]))).loc[0,"n"]) > 0:
                    st.error("Ese email ya está en uso.")
                else:
                    if new_pwd:
                        run_script("UPDATE users SET email=?, role=?, active=?, password=? WHERE id=?",
                                  (new_email, new_role, 1 if new_active else 0, hash_password(new_pwd), int(row["id"])))
                    else:
                        run_script("UPDATE users SET email=?, role=?, active=? WHERE id=?",
                                  (new_email, new_role, 1 if new_active else 0, int(row["id"])))
                    st.success("Usuario actualizado."); st.rerun()

    with tabs[1]:
        st.markdown("### Servicios")
        s = run_query("SELECT id, name FROM services ORDER BY name")
        st.dataframe(s, use_container_width=True)
        new_s = st.text_input("Nuevo servicio", key="svc_new")
        if st.button("Crear servicio", key="btn_svc_create"): 
            run_script("INSERT INTO services(name) VALUES(?)", (new_s,)); st.rerun()

        st.markdown("### SLAs por Prioridad")
        if not s.empty:
            svc = st.selectbox("Servicio", s["name"], key="sla_svc")
            sid = int(s[s["name"]==svc]["id"].iloc[0])
            df = run_query("SELECT priority, response_hours, resolve_hours FROM service_sla WHERE service_id=?", (sid,))
            st.dataframe(df, use_container_width=True)
            c1,c2,c3 = st.columns(3)
            with st.form("form_sla"):
                pr = c1.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"], key="sla_pr")
                rh = c2.number_input("Horas respuesta", min_value=1, step=1, key="sla_rh")
                oh = c3.number_input("Horas resolución", min_value=1, step=1, key="sla_oh")
                if st.form_submit_button("Guardar SLA", use_container_width=True):
                    ex = run_query("SELECT COUNT(*) n FROM service_sla WHERE service_id=? AND priority=?", (sid, pr))
                    if int(ex.loc[0,"n"])>0:
                        run_script("UPDATE service_sla SET response_hours=?, resolve_hours=? WHERE service_id=? AND priority=?", (int(rh), int(oh), sid, pr))
                    else:
                        run_script("INSERT INTO service_sla(service_id,priority,response_hours,resolve_hours) VALUES(?,?,?,?)", (sid, pr, int(rh), int(oh)))
                    st.success("SLA guardado."); st.rerun()

    with tabs[2]:
        st.markdown("### Matriz Urgencia × Impacto por servicio")
        s = run_query("SELECT id, name FROM services ORDER BY name")
        if s.empty: st.info("Crea servicios primero.")
        else:
            svc = st.selectbox("Servicio", s["name"], key="mx_svc")
            sid = int(s[s["name"]==svc]["id"].iloc[0])
            df = run_query("SELECT urgency, impact, priority FROM service_matrix WHERE service_id=? ORDER BY urgency, impact", (sid,))
            st.dataframe(df, use_container_width=True)
            with st.form("form_mx"):
                u = st.selectbox("Urgencia", ["Baja","Media","Alta"], key="mx_u")
                i = st.selectbox("Impacto", ["Bajo","Medio","Alto"], key="mx_i")
                p = st.selectbox("Prioridad", ["Baja","Media","Alta","Crítica"], key="mx_p")
                if st.form_submit_button("Guardar regla", use_container_width=True):
                    ex = run_query("SELECT COUNT(*) n FROM service_matrix WHERE service_id=? AND urgency=? AND impact=?", (sid, u, i))
                    if int(ex.loc[0,"n"])>0:
                        run_script("UPDATE service_matrix SET priority=? WHERE service_id=? AND urgency=? AND impact=?", (p, sid, u, i))
                    else:
                        run_script("INSERT INTO service_matrix(service_id,urgency,impact,priority) VALUES(?,?,?,?)", (sid, u, i, p))
                    st.success("Matriz guardada."); st.rerun()

    with tabs[3]:
        st.markdown("### Aprobaciones por Servicio/Área (workflow para Cambios)")
        s = run_query("SELECT id, name FROM services ORDER BY name")
        a = run_query("SELECT id, name FROM areas ORDER BY name")
        st.dataframe(a, use_container_width=True)
        with st.form("form_area"):
            an = st.text_input("Nueva área", key="area_new")
            if st.form_submit_button("Crear área", use_container_width=True):
                run_script("INSERT INTO areas(name) VALUES(?)", (an,)); st.rerun()
        if s.empty or a.empty: st.info("Crea servicios y áreas antes de definir niveles.")
        else:
            svc = st.selectbox("Servicio", s["name"], key="appr_svc")
            sid = int(s[s["name"]==svc]["id"].iloc[0])
            area = st.selectbox("Área", a["name"], key="appr_area")
            aid = int(a[a["name"]==area]["id"].iloc[0])
            level = st.number_input("Nivel", min_value=1, step=1, key="appr_level")
            if st.button("Agregar nivel", key="btn_add_level"):
                run_script("INSERT OR IGNORE INTO service_area_levels(service_id,area_id,level) VALUES(?,?,?)", (sid, aid, int(level)))
                st.success("Nivel agregado."); st.rerun()
            flow = run_query("""SELECT l.level, ar.name as area FROM service_area_levels l JOIN areas ar ON ar.id=l.area_id
                                WHERE l.service_id=? ORDER BY l.level""", (sid,))
            st.dataframe(flow, use_container_width=True)

    with tabs[4]:
        st.markdown("### Notificaciones y SSO")
        st.subheader("SMTP (persistente en DB)")
        with st.form("smtp_cfg_form"):
            host = st.text_input("SMTP Host", value=get_setting("smtp_host",""), key="smtp_host")
            port = st.number_input("SMTP Port", min_value=1, max_value=65535, value=int(get_setting("smtp_port","587") or 587), key="smtp_port")
            userv = st.text_input("SMTP User", value=get_setting("smtp_user",""), key="smtp_user")
            pwdv  = st.text_input("SMTP Password", value=get_setting("smtp_password",""), type="password", key="smtp_pwd")
            fromv = st.text_input("From (opcional)", value=get_setting("smtp_from",""), key="smtp_from")
            ok = st.form_submit_button("Guardar SMTP", use_container_width=True)
        if ok:
            set_setting("smtp_host", host.strip())
            set_setting("smtp_port", str(int(port)))
            set_setting("smtp_user", userv.strip())
            set_setting("smtp_password", pwdv.strip())
            set_setting("smtp_from", fromv.strip())
            st.success("SMTP guardado.")
        st.markdown("---")
        st.subheader("Probar envío")
        test_to = st.text_input("Enviar correo de prueba a:", key="smtp_test_to")
        if st.button("Enviar prueba", key="btn_smtp_test", type="primary"):
            if send_email(test_to, "Prueba SMTP", "Este es un correo de prueba de la Mesa de Ayuda."):
                st.success("¡Enviado! Revisa tu bandeja.")
            else:
                st.error("No se pudo enviar. Verifica la configuración.")
        st.markdown("---")
        st.write("- **SSO Token**: usa `SSO_SHARED_SECRET` y URL con `?user=<u>&ts=<unix>&sig=<hmac>`")
        st.write("- **Webhooks**: define SLACK_WEBHOOK_URL / TEAMS_WEBHOOK_URL / DISCORD_WEBHOOK_URL (variables de entorno)")

def router():
    user = get_current_user()
    if not user:
        page_login(); return
    page = sidebar_menu()
    if page == "Dashboard":
        st.header("Dashboard")
        t = run_query("SELECT status, COUNT(*) n FROM tickets GROUP BY status ORDER BY 2 DESC")
        st.dataframe(t, use_container_width=True)
    elif page == "Tickets – Nuevo": page_tickets_nuevo()
    elif page == "Tickets – Bandeja": page_tickets_bandeja()
    elif page == "Ticket – Detalle": page_ticket_detalle()
    elif page == "Activos": page_activos()
    elif page == "CMDB": page_cmdb()
    elif page == "Mi Perfil y Seguridad": page_mi_perfil_seguridad()
    elif page == "Configuración": page_configuracion()
    else: st.stop()

def main():
    st.set_page_config(page_title="Mesa de Ayuda + Inventarios (Enterprise+)", layout="wide")
    router()

if __name__ == "__main__":
    main()
