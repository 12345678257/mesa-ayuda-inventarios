FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Puerto estándar; Railway proporciona $PORT
EXPOSE 8000

CMD ["bash", "-lc", "streamlit run app_mesa_ayuda_inventarios_streamlit.py --server.port ${PORT:-8000} --server.address 0.0.0.0"]
