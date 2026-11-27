# vigil on a server (Railway, any Docker host). State lives on a volume mounted at /data.
FROM python:3.12-slim
WORKDIR /app
COPY vigil ./vigil
COPY config.railway.toml ./
RUN pip install --no-cache-dir certifi tzdata
ENV PYTHONUNBUFFERED=1 VIGIL_TZ=America/New_York
CMD ["python", "-m", "vigil", "run", "config.railway.toml"]
