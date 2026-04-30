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
RUN pip install --no-cache-dir --no-deps xgboost==2.1.4

# Copy application code + shared packages used by dashboard
COPY market_data_dashboard /app/market_data_dashboard
COPY contracts_app /app/contracts_app
COPY ingestion_app /app/ingestion_app
COPY snapshot_app /app/snapshot_app
COPY strategy_app /app/strategy_app
COPY ml_pipeline_2 /app/ml_pipeline_2

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
