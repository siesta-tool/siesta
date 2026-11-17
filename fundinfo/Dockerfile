FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app

COPY requirements.txt requirements.txt
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r api/requirements.txt

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${API_PORT:-8000}"]
