# Mesa de Ayuda e Inventarios (ITIL)
Ejecuta local:
```bash
pip install -r requirements.txt
streamlit run app_mesa_ayuda_inventarios_streamlit.py
```
Usuario inicial: `admin / admin`

## Variables para producción
- `APP_DB_PATH` → ruta del .db en el volumen persistente.
- `APP_SMTP_PASSWORD` → clave SMTP (opcional si también configuras en la UI).

## Render
Usa `render.yaml` (Blueprint) y monta un disco en `/var/data`:
- `APP_DB_PATH=/var/data/inventarios_helpdesk.db`

## Railway
Usa `Dockerfile`, crea un Volume en `/data` y define:
- `APP_DB_PATH=/data/inventarios_helpdesk.db`
