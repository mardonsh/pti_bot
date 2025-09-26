FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY dashboard ./dashboard

ENV UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000

CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
