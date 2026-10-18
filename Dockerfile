FROM python:3.12-slim

# ffmpeg: MP3 encoding and chapters. tesseract: OCR for scanned pages
# (English, Ancient Greek, Modern Greek, Latin, German, French, Italian).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-grc tesseract-ocr-ell \
        tesseract-ocr-lat tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-ita \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

ENV DATA_DIR=/data \
    OCR_LANGS=eng+grc+ell+lat \
    PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
