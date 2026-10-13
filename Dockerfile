FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
USER app
WORKDIR /srv/backend
ENV DATABASE_URL=sqlite:////data/workbench.db FRONTEND_DIR=/srv/frontend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready').status==200 else 1)"
# One worker: SQLite has a single writer and every transaction takes the write lock anyway.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
