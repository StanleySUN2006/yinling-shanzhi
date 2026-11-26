FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY . .
ENV PYTHONUNBUFFERED=1 \
    YINLING_PUBLIC=1 \
    BIND_HOST=0.0.0.0
EXPOSE 10000
CMD python server.py --port ${PORT:-10000}
