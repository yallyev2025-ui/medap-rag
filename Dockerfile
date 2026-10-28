# Образ MedAP Student AI: Telegram-бот + /v1 API + админка в одном процессе.
#
# Собирается на Timeweb App Platform (тип сборки Dockerfile). Nixpacks там нет,
# поэтому системные пакеты OCR, которые раньше ставились через nixpacks.toml,
# переехали сюда.
FROM python:3.12-slim

# - tesseract-ocr + rus/eng — распознавание текста со сканов учебников
# - poppler-utils — pdf2image конвертирует страницы PDF в картинки (pdftoppm)
# - curl — healthcheck контейнера
# - libfontconfig1 — нужен skia-canvas (PDF презентаций в админке, «Стенд презентаций»)
# Повторы при скачивании: DNS сборщика Timeweb периодически не отвечает, одна
# неудачная попытка не должна валить весь деплой.
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        poppler-utils \
        curl \
        libfontconfig1 \
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
RUN pip install --retries 10 --timeout 60 --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Прогрев весов на этапе сборки: эмбеддер (~2.2 ГБ) попадает в слой образа, и старт контейнера не зависит от доступности huggingface.co.
# Большой реранкер (~2.3 ГБ) в образ НЕ кладётся (28.10.2026): его скачивают только для сжатия (ниже) и удаляют в том же шаге.
# Если сборщик до HF не достучится — задать HF_ENDPOINT (например, зеркало) и пересобрать.
ARG EMBEDDING_MODEL_NAME=intfloat/multilingual-e5-large
ARG RERANKER_MODEL_NAME=BAAI/bge-reranker-v2-m3
ARG KEEP_TORCH_RERANKER=0
RUN for attempt in 1 2 3; do \
        python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('${EMBEDDING_MODEL_NAME}')" && exit 0; \
        echo "Загрузка эмбеддера не удалась (попытка $attempt), повтор через 20 с"; sleep 20; \
    done; exit 1
# KEEP_TORCH_RERANKER=1 — положить большой реранкер в образ (запасной путь RERANKER_BACKEND=torch)
RUN if [ "$KEEP_TORCH_RERANKER" = "1" ]; then python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANKER_MODEL_NAME}')"; fi

# Реранкер int8/ONNX (RERANKER_BACKEND=onnx — теперь по умолчанию): скачать большую модель, сжать, подобрать порог (threshold.json рядом с моделью —
# бот читает его сам), удалить большую — всё одним шагом (scripts/build_reranker.sh), поэтому в слой образа остаётся только сжатая (~0.6 ГБ).
# Конвертации нужно ~8 ГБ памяти и несколько минут. Не получилось сжать — сборка падает (если не KEEP_TORCH_RERANKER=1).
# Версии optimum/optimum-onnx закреплены вместе с transformers (та же версия, что в requirements.txt): без этого pip перебирал версии и сборка висела (23.10.2026).
# optimum-onnx 0.1.0 требует optimum~=2.1.0 и transformers<4.58; при смене версий проверяйте эти границы на pypi.org.
# Пресет avx2 работает на любом x86; с avx512_vnni (проверка: lscpu | grep avx512_vnni) быстрее.
ARG BUILD_ONNX_RERANKER=1
ARG ONNX_PRESET=avx2
COPY scripts/export_reranker_onnx.py scripts/calibrate_onnx_threshold.py scripts/build_reranker.sh /tmp/
RUN if [ "$BUILD_ONNX_RERANKER" = "1" ]; then \
        ONNX_PRESET="$ONNX_PRESET" KEEP_TORCH_RERANKER="$KEEP_TORCH_RERANKER" RERANKER_MODEL_NAME="$RERANKER_MODEL_NAME" sh /tmp/build_reranker.sh; \
    fi

# Движок вёрстки презентаций (Medap Slide) для страницы админки «Стенд презентаций»: Node.js 22 из
# официального архива (в apt-версии Node и npm старые) и зависимости движка в /opt/slides-engine.
# Стоит ПОСЛЕ тяжёлых слоёв (torch, веса моделей): правка кода их не пересобирает.
ARG NODE_VERSION=22.11.0
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" \
        | tar -xz -C /usr/local --strip-components=1 --exclude='*/share/doc' --exclude='*/CHANGELOG.md' \
    && node -v && npm -v
COPY slides-engine/package.json slides-engine/package-lock.json /opt/slides-engine/
RUN cd /opt/slides-engine && npm ci --omit=dev --no-audit --no-fund
COPY slides-engine/dist /opt/slides-engine/dist
COPY slides-engine/fonts /opt/slides-engine/fonts
COPY slides-engine/render-drafts.mjs /opt/slides-engine/render-drafts.mjs

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "-m", "bot.main"]
