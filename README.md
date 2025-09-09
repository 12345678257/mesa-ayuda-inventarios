# Help Desk & Inventarios – ITIL Enterprise+
**Incluye:** Incidente, Solicitud, Cambio (RFC), Problema; SLA por **políticas**, **matriz Urgencia×Impacto→Prioridad** (global y **por servicio**), **catálogo de servicios jerárquico**, **CMDB (CI + relaciones)**, **aprobaciones multinivel** (Manager, CAB y **áreas configurables**), **calendario de cambios**, **kanban por agente**, **base de conocimiento**, **CSAT/CES/NPS**, **vistas por equipo**, **webhooks** y **SSO por token**; además: **recuperar/cambiar contraseña**, **inventario con kárdex** y **productos Administrativos/Asistenciales**.

## Local
```bash
pip install -r requirements.txt
streamlit run app_mesa_ayuda_inventarios_streamlit.py
```
Credenciales iniciales: `admin / admin`

## Variables
- `APP_DB_PATH` → ruta del .db en el volumen persistente.
- `APP_SMTP_PASSWORD` → clave SMTP (opcional si también configuras en la UI).
- `APP_SSO_SECRET` → secreto para SSO por token (HMAC SHA256). URL: `?user=<usuario>&ts=<epoch>&sig=hex(hmac(secret, f"{user}:{ts}"))`

## Render
Usa `render.yaml` con disco en `/var/data` y define:
- `APP_DB_PATH=/var/data/inventarios_helpdesk.db`

## Railway
Usa `Dockerfile`, crea un Volume en `/data` y define:
- `APP_DB_PATH=/data/inventarios_helpdesk.db`
