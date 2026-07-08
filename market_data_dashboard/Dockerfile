FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY market_data_dashboard/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Must match the xgboost version models are TRAINED with (ml_pipeline_2 → 3.2.0).
# A 3.2.0-pickled model loaded under 2.1.4 is silently mis-evaluated (wrong probs).
RUN pip install --no-cache-dir --no-deps xgboost==3.2.0

# Copy application code + shared packages used by dashboard
COPY market_data_dashboard /app/market_data_dashboard
COPY contracts_app /app/contracts_app
COPY ingestion_app /app/ingestion_app
COPY snapshot_app /app/snapshot_app
COPY strategy_app /app/strategy_app
COPY ml_pipeline_2 /app/ml_pipeline_2
# execution_app: this image also runs the seller_app service (python -m strategy_app.seller
# via docker-compose.seller.yml), whose real-money gateway does
# `from execution_app.adapter.dhan import DhanAdapter` and whose trade alerts do
# `from execution_app.alerts import _send_telegram` — without this, SELLER_LIVE_ENABLED=1
# fails to import at the exact moment it's needed (found 2026-07-08, same bug class as
# the strategy_app image before commit ba9cfb8).
COPY execution_app /app/execution_app

# Create non-root user
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Expose port
EXPOSE 8008

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8008/api/health', timeout=5)"

# Run the application
WORKDIR /app/market_data_dashboard
ENV PYTHONPATH=/app
CMD ["python", "app.py"]
