# The familiar's server, for tier 3 (an always-on box). See docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# One Piper voice baked into the image. Override at build time with
#   docker compose build --build-arg PIPER_VOICE=en_GB-cori-high
ARG PIPER_VOICE=en_GB-alan-medium
RUN mkdir -p voices && python -m piper.download_voices --data-dir voices "$PIPER_VOICE"

COPY . .

# Runs as root inside the container so the bind-mounted ./data is writable
# whoever created it on the host. Moving a table here from a laptop is copying
# its data/ folder into ./data beside docker-compose.yml.
EXPOSE 8321
VOLUME ["/app/data"]

# The same flags as deploy/familiar.service: ONE worker (per-table state is
# in-process), threads as the dial, since every connected body holds one.
CMD ["gunicorn", "-k", "gthread", "-w", "1", "--threads", "64", "--timeout", "120", \
     "-b", "0.0.0.0:8321", "server:app"]
