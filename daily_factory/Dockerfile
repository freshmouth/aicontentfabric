FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY daily_factory /app/daily_factory
COPY pipeline_google_omni_stack /app/pipeline_google_omni_stack
COPY social_scheduler /app/social_scheduler
COPY characters/claire_natural/master_reference.png /app/characters/claire_natural/master_reference.png

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "daily_factory.worker"]
