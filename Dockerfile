FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY tfda_context_gate/requirements.txt /app/tfda_context_gate/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/tfda_context_gate/requirements.txt

COPY . /app

RUN mkdir -p /app/data/processed \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app/data /app/tfda_context_gate/data/processed

USER appuser
EXPOSE 8080

CMD ["sh", "-c", "uvicorn line_bot.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
