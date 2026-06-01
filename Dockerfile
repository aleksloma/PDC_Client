# PowerDataChat Client — enterprise (on-premise, customer's LAN)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_ROOT=/data/client \
    BRAIN_URL=http://brain:8080

# Native libs for matplotlib / kaleido / pandas
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        libxml2 libgomp1 fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/client /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
