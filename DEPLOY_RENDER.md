# Despliegue en Render (con disco persistente)

## Opción A: Usar `render.yaml` (recomendado)
1. Conecta tu repo a Render.
2. Render detectará `render.yaml` y creará el servicio **web**:
   - Build: `pip install -r requirements.txt`
   - Start: `streamlit run app_mesa_ayuda_inventarios_streamlit.py --server.port $PORT --server.address 0.0.0.0`
   - Disco: montado en `/var/data` (1 GB)
   - Env: `APP_DB_PATH=/var/data/inventarios_helpdesk.db`
3. En **Environment** añade `APP_SMTP_PASSWORD` como Secret si vas a enviar correos.
4. Deploy.

## Opción B: Crear el servicio manualmente
- Type: **Web Service**
- Env: **Python**
- Build Command: `pip install -r requirements.txt`
- Start Command:
```
streamlit run app_mesa_ayuda_inventarios_streamlit.py --server.port $PORT --server.address 0.0.0.0
```
- Disks: añade un **Disk** y móntalo en `/var/data` (1–10 GB según necesites).
- Env Var: `APP_DB_PATH=/var/data/inventarios_helpdesk.db`
- Secret: `APP_SMTP_PASSWORD` (opcional).

Tras el deploy, entra a la app y configura SMTP en **Configuración → Notificaciones**.
