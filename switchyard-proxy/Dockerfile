# renovate: datasource=docker depName=docker.io/library/python
FROM python:3.12-slim

RUN pip install --no-cache-dir "nemo-switchyard==0.2.0"

WORKDIR /app
COPY routes.toml /app/routes.toml
COPY server.py /app/server.py

EXPOSE 4000

ENV SWITCHYARD_CONFIG=/app/routes.toml
ENV SWITCHYARD_PORT=4000

ENTRYPOINT ["python3", "/app/server.py"]
