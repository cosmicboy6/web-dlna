FROM python:3.12-slim

LABEL org.opencontainers.image.title="web-dlna" \
      org.opencontainers.image.description="DLNA/UPnP media server with prebuilt multi-source MP3 indexes and download manager" \
      org.opencontainers.image.source="https://github.com/cosmicboy6/web-dlna" \
      org.opencontainers.image.version="1.0.0"

WORKDIR /app

COPY app.py /app/app.py
COPY web/ /app/web/

# Nur Index-Metadaten, keine Audio-Dateien.
COPY data/sources.json /index/sources.json
COPY data/library.json.gz /index/library.json.gz

ENV TZ=Europe/Berlin \
    FRADGIFY_INDEX_DIR=/index

EXPOSE 9092

CMD ["python3", "-u", "/app/app.py"]
