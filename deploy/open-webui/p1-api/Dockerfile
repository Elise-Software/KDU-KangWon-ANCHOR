FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace/scripts:/app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && if [ "$(uname -m)" = "aarch64" ]; then \
         pip install "torch>=2.6,<3"; \
       else \
         pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.6,<3"; \
       fi \
    && pip install -r /app/requirements.txt

COPY app.py audit.py raw_proxy.py /app/
RUN useradd --create-home --uid 10001 wonju \
    && mkdir -p /workspace/.cache /var/lib/wonju-audit \
    && chown -R wonju:wonju /app /workspace /var/lib/wonju-audit
USER wonju

EXPOSE 8010
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
