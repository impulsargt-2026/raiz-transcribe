# RAÍZ Transcribe — servidor en la nube (cualquier host de contenedores: Render, Railway, etc.)
FROM python:3.12-slim
# DejaVu: fuente Unicode para el PDF (tildes, ñ, ¿¡). FFmpeg viene dentro de imageio-ffmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Secretos (NO van acá): ASSEMBLYAI_API_KEY, RAIZ_TOKEN y, si no hay disco persistente, HF_TOKEN + RAIZ_HF_REPO
ENV RAIZ_CLOUD=1 RAIZ_DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONUTF8=1
RUN mkdir -p /data
# --no-access-log: las URLs llevan ?k=CLAVE y no deben quedar en los logs del host
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
