# Mesa de Ayuda + Inventarios (ITIL 4) — Enterprise+

- Tickets con ITIL (Incidente, Solicitud, Cambio, Problema)
- Matriz Urgencia×Impacto por servicio → prioridad sugerida
- SLA por servicio/prioridad (respuesta y resolución)
- Aprobaciones multinivel (Cambios) por servicio/área
- Encuestas CSAT/CES/NPS al cierre
- Vistas por equipo
- Activos: hoja de vida (asignaciones/mantenimientos), pólizas, contratos, garantía, depreciación Fiscal/NIIF, export XLSX
- Adjuntos en tickets y activos (generales y por mantenimiento/póliza/contrato) + visor PDF/imagen
- SMTP configurable (persistente) + prueba de envío
- Webhooks (Slack/Teams/Discord), SSO por token HMAC

## Variables
- APP_DB_PATH (p. ej. /var/data/inventarios_helpdesk.db)
- SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD (opcional, si no usas Configuración)
- SSO_SHARED_SECRET (SSO por token)
- SLACK_WEBHOOK_URL / TEAMS_WEBHOOK_URL / DISCORD_WEBHOOK_URL (opcional)
