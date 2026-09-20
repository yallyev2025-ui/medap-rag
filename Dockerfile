# Образ MedAP Student AI: Telegram-бот + /v1 API + админка в одном процессе.
#
# Собирается на Timeweb App Platform (тип сборки Dockerfile). Nixpacks там нет,
# поэтому системные пакеты OCR, которые раньше ставились через nixpacks.toml,
# переехали сюда.
FROM python:3.12-slim

# - tesseract-ocr + rus/eng — распознавание текста со сканов учебников
# - poppler-utils — pdf2image конвертирует страницы PDF в картинки (pdftoppm)
# - curl — healthcheck контейнера
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Веса моделей кладём в образ, а не в кэш пользователя: файловая система
    # приложения на App Platform эфемерная, иначе каждый рестарт качал бы ~4.5 ГБ.
    HF_HOME=/opt/models \
    PORT=8000

# torch по умолчанию тянет CUDA-колёса (~2.5 ГБ лишнего веса в образе), а GPU
# на App Platform нет — ставим CPU-сборку с отдельного индекса.
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Прогрев весов на этапе сборки: эмбеддер (~2.2 ГБ) и реранкер (~2.3 ГБ) попадают
# в слой образа, и старт контейнера не зависит от доступности huggingface.co.
# Если сборщик до HF не достучится — задать HF_ENDPOINT (например, зеркало) и пересобрать.
ARG EMBEDDING_MODEL_NAME=intfloat/multilingual-e5-large
ARG RERANKER_MODEL_NAME=BAAI/bge-reranker-v2-m3
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('${EMBEDDING_MODEL_NAME}'); \
CrossEncoder('${RERANKER_MODEL_NAME}')"

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "-m", "bot.main"]
