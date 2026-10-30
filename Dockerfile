FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY *.py ./
RUN if [ ! -f app.py ]; then ln -s main.py app.py; fi

# The listening port comes from the platform's PORT env var at runtime
# (app.py: SERVER_PORT -> PORT -> 3000). No EXPOSE on purpose: a baked-in
# port would contradict whatever the platform injects.

CMD ["python", "app.py"]
