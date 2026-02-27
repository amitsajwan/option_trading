FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY persistence_app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY persistence_app /app/persistence_app

ENV PYTHONPATH=/app

CMD ["python", "-m", "persistence_app.main_snapshot_consumer", "--foreground"]
