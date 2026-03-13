FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY strategy_app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY ml_pipeline /app/ml_pipeline
COPY ml_pipeline_2/src /app/ml_pipeline_2/src
COPY strategy_app /app/strategy_app

ENV PYTHONPATH=/app:/app/ml_pipeline/src:/app/ml_pipeline_2/src

CMD ["python", "-m", "strategy_app.main", "--engine", "deterministic"]
