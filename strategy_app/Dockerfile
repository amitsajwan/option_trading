FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY strategy_app/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps xgboost==2.1.4

COPY contracts_app /app/contracts_app
COPY ml_pipeline_2 /app/ml_pipeline_2
COPY snapshot_app /app/snapshot_app
COPY strategy_app /app/strategy_app

ENV PYTHONPATH=/app

CMD ["python", "-m", "strategy_app.main", "--engine", "deterministic"]
