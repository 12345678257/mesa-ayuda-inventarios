# Mesa de Ayuda + Inventarios (ITIL 4) — Enterprise+ (Streamlit)

- Roles: **admin**, **agente**, **usuario**
- ITIL Tabs: Incidente, Solicitud, Cambio, Problema
- Agente puede **cambiar estado** y **cerrar** tickets
- Notificaciones por email al **crear/asignar/cambiar estado**
- Activos con **Hoja de vida** (asignaciones, mantenimientos) y **Depreciación** (línea recta)
- Páginas:
  - **Tickets – Nuevo**, **Bandeja**, **Detalle**
  - **Activos & CMDB** (básico)
  - **Mi Perfil y Seguridad** (cambio de contraseña)
  - **Configuración** (seguridad, ajustes)

## Variables de entorno
- `APP_DB_PATH` (ruta absoluta de la base SQLite). Ej: `/var/data/inventarios_helpdesk.db`
- SMTP opcional: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`

## Inicio local
```bash
pip install -r requirements.txt
streamlit run app_mesa_ayuda_inventarios_streamlit.py
```
