FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home runner
COPY app.py __init__.py ./runner/
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn runner.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
