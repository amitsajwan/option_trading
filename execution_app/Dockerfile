FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY execution_app/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts_app /app/contracts_app
COPY strategy_app /app/strategy_app
COPY execution_app /app/execution_app

ENV PYTHONPATH=/app

CMD ["python", "-m", "execution_app"]
