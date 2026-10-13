FROM python:3.12-slim

RUN pip install --no-cache-dir "docker>=7,<8"

WORKDIR /app
COPY gateway.py /app/gateway.py

EXPOSE 9000
CMD ["python3", "-u", "/app/gateway.py"]
