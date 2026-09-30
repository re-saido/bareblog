FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BLOG_HOST=0.0.0.0 \
    BLOG_PORT=8000 \
    BLOG_FILES=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run unprivileged. /data is created here, owned by that user, so a named
# volume mounted over it starts out writable.
RUN useradd --system --uid 10001 --no-create-home blog \
    && mkdir /data && chown blog:blog /data
COPY server/ ./
USER blog

VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=2)"
CMD ["python", "server.py"]
