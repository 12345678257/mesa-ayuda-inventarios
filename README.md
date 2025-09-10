# Mesa de Ayuda + Inventarios (ITIL 4) — Enterprise+ (Streamlit)

Incluye:
- **SSO** por token HMAC (query `?user=...&ts=...&sig=...`) y preparación para OAuth/OIDC.
- **Aprobaciones multinivel por servicio/área** (workflow para Cambios).
- **Matriz urgencia×impacto por servicio** configurable (sugiere prioridad).
- **SLA por servicio y prioridad** (respuesta y resolución).
- **Encuestas** CSAT/CES/NPS al cierre.
- **Vistas por equipo** (teams, team_members).
- **Activos (Hoja de Vida avanzada)**: asignaciones, mantenimientos, pólizas, contratos, garantía; depreciación **Fiscal** y **NIIF** con reporte XLSX.
- **Webhooks** (Slack/Teams/Discord) para eventos: crear, asignar, cambio de estado, aprobación.
- **Notificaciones por email** (opcional SMTP).
- **Interfaz con color y botones**.

## Variables de entorno
- `APP_DB_PATH` (requerido) → p.ej. `/var/data/inventarios_helpdesk.db`
- SMTP opcional: `SMTP_HOST`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD`
- SSO token: `SSO_SHARED_SECRET`
- Webhooks opcionales: `SLACK_WEBHOOK_URL`, `TEAMS_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL`

## Local
```bash
pip install -r requirements.txt
streamlit run app_mesa_ayuda_inventarios_streamlit.py
```

## Render/Railway
Usa `Dockerfile` + `render.yaml`. Define `APP_DB_PATH` hacia el disco/volume.
