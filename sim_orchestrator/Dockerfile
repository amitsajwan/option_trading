FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY sim_orchestrator/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY ops /app/ops
COPY sim_orchestrator /app/sim_orchestrator

ENV PYTHONPATH=/app
ENV REPO_ROOT=/opt/option_trading

CMD ["python", "-m", "sim_orchestrator.main"]
