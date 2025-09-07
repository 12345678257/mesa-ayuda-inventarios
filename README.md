# Mesa de Ayuda e Inventarios (Streamlit)

## Ejecutar localmente
```bash
pip install -r requirements.txt
streamlit run app_mesa_ayuda_inventarios_streamlit.py
```

## Desplegar en Streamlit Community Cloud
1. Sube este repo a GitHub con estos archivos.
2. En https://share.streamlit.io → **Create app** → selecciona repo/branch/archivo.
3. En **Advanced settings → Secrets**, pega:
   ```toml
   APP_SMTP_PASSWORD="tu_password_smtp"
   ```
4. Deploy. Luego configura SMTP en **Configuración → Notificaciones**.


## Despliegue con disco persistente
- Ver **DEPLOY_RENDER.md** (Render)
- Ver **DEPLOY_RAILWAY.md** (Railway)
