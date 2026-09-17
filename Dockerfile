FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and prompts
COPY --chown=app:app backend/ /app/backend/
COPY --chown=app:app prompts/ /app/prompts/

RUN mkdir -p /app/backend/data /app/backend/logs && chown -R app:app /app/backend/data /app/backend/logs

USER app

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
