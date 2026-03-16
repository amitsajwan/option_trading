FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY snapshot_app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY snapshot_app /app/snapshot_app

ENV PYTHONPATH=/app

CMD ["python", "-m", "snapshot_app.main_live", "--foreground"]
