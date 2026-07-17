FROM python:3.12-alpine

WORKDIR /app
RUN pip install --no-cache-dir "requests>=2.32,<3"
COPY bootstrap_permissions.py /app/bootstrap_permissions.py
RUN adduser -D -u 10001 wonju && chown -R wonju:wonju /app
USER wonju

CMD ["python", "/app/bootstrap_permissions.py"]
