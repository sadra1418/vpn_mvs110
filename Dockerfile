FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

# gthread: each worker serves many requests at once, so a page with dozens of
# assets (or a long video stream) does not block the other visitors.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT} --worker-class gthread --workers 2 --threads 16 --timeout 120 --keep-alive 5"]
