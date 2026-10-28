# Novus kiln dashboard — container image (OPTIONAL path).
#
# The native install is recommended:  bash install.sh  (see README). This
# image exists for people who already run everything in Docker. Bluetooth
# only works when the container shares the host's BlueZ D-Bus socket and
# network namespace — see docker-compose.yml for the required settings.
#
#   docker compose up -d          # dashboard on :8501, API on :8000
#   docker compose logs -f dashboard

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# bleak talks to BlueZ over D-Bus; it needs the glib runtime and the bluez
# client tooling (bluetoothctl is handy for debugging pairing from inside).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 bluez dbus \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so code changes don't invalidate the pip layer.
COPY requirements.txt requirements-api.txt ./
RUN pip install -r requirements.txt -r requirements-api.txt

COPY . .

EXPOSE 8501 8000

# Dashboard by default; docker-compose.yml overrides this for the api service.
CMD ["streamlit", "run", "kiln_dashboard.py", \
     "--server.address", "0.0.0.0", \
     "--server.port", "8501", \
     "--server.headless", "true"]
