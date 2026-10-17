# Twin backend (FastAPI + WebSocket) for Railway. The web app is on Vercel (web/).
FROM python:3.12-slim

# git: clone connected repos. openscad + blender: CAD 3D pipeline (server/cad).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openscad blender ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ server/
COPY demo_project/ demo_project/

ENV PYTHONUNBUFFERED=1 \
    TWIN_STATE_DIR=/data \
    OPENSCAD_BIN=/usr/bin/openscad \
    BLENDER_BIN=/usr/bin/blender
WORKDIR /app/server
# Railway injects PORT. Secrets (CRUSOE_API_KEY, NVIDIA_API_KEY, TYPESAFE_API_KEY, JIRA_*, WORKSPACES...) come from Railway variables.
CMD ["sh", "-c", "mkdir -p /data && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --ws-ping-interval 20 --ws-ping-timeout 30"]
