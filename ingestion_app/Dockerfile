FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY ingestion_app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY ingestion_app /app/ingestion_app
COPY market_data/src /app/market_data/src

ENV PYTHONPATH=/app:/app/market_data/src

EXPOSE 8004

CMD ["python", "-m", "ingestion_app.market_session_runner", "--mode", "live"]
