# Start with a lightweight Python Linux environment
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (ADDED poppler-utils and tesseract-ocr)
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre-headless \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    libcairo2 \
    libffi-dev \
    shared-mime-info \
    qpdf \
    gcc \
    g++ \
    pkg-config \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# CRITICAL OOM FIX: Changed --workers 2 to --workers 1.
# This dedicates 100% of the server's RAM to a single task, preventing FontTools from crashing.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --timeout-keep-alive 2700"]