FROM python:3.12-slim

WORKDIR /app

# docker CLI (for `docker logs -f` subprocess in audit_tail)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    curl -fsSL https://get.docker.com | sh && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "main.py"]
