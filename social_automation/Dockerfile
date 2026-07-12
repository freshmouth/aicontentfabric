FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DELIVERY_STORE=firestore \
    PORT=8080

WORKDIR /app
COPY requirements.txt /app/social_automation/requirements.txt
RUN pip install --no-cache-dir -r /app/social_automation/requirements.txt
COPY . /app/social_automation

CMD exec gunicorn --bind :${PORT} --workers 2 --threads 4 --timeout 120 social_automation.service:app
