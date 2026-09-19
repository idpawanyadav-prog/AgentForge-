# ---------- Frontend build ----------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---------- Backend runtime ----------
FROM python:3.12-slim AS runtime
WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=frontend-build /app/frontend/dist ./static

# Runtime state (SQLite DB + gateway-key master key) lives outside the
# copied source tree so a stray local db/key file can never ship in an
# image layer. Override via environment if persistence is mounted.
ENV PYTHONUNBUFFERED=1 \
    AGENT_OFFICE_DB=/data/agent_office.db \
    AGENT_OFFICE_KEYFILE=/data/.gateway_keys.key
RUN mkdir -p /data
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --app-dir backend"]
