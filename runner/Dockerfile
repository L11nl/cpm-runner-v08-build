FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home runner
COPY app.py bootstrap.py __init__.py ./runner/
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn runner.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
