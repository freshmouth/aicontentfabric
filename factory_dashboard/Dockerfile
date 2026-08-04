FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTORY_ROOT=/app \
    PORT=8080

WORKDIR /app

COPY factory_dashboard/requirements.txt /app/factory_dashboard/requirements.txt
RUN pip install --no-cache-dir -r /app/factory_dashboard/requirements.txt

COPY factory_dashboard /app/factory_dashboard
COPY accounts /app/accounts

RUN useradd --create-home --uid 10001 dashboard \
    && chown -R dashboard:dashboard /app

USER dashboard
EXPOSE 8080

CMD ["sh", "-c", "uvicorn factory_dashboard.app.main:app --host 0.0.0.0 --port ${PORT}"]
