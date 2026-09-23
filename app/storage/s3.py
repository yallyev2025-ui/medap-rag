"""S3-хранилище оригиналов источников и постраничного текста (§8.2.4, §33 ТЗ).

Мягкая деградация: без заданных S3_* переменных модуль просто ничего не
сохраняет (is_configured() == False) — чанки и эмбеддинги от этого не
страдают, теряется только возможность переиндексации без повторной загрузки
файла и подсветка фрагмента в Source Viewer (этап 3).
"""

import hashlib
import logging
from functools import lru_cache
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        settings.S3_ENDPOINT_URL and settings.S3_BUCKET and settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY
    )


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    )


def file_checksum(file_path: str) -> str:
    """SHA-256 файла — часть ключа объекта в S3 и признак неизменности оригинала."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def upload_original(file_path: str, book_id: int, checksum: str, extension: str) -> str | None:
    """Загружает оригинал источника. Возвращает ключ объекта или None при
    отсутствии конфигурации/ошибке — вызывающий код не должен из-за этого
    прерывать загрузку учебника."""
    if not is_configured():
        return None
    key = f"sources/{book_id}/{checksum}{extension}"
    try:
        _client().upload_file(file_path, settings.S3_BUCKET, key)
    except Exception:
        logger.exception("Не удалось загрузить оригинал источника в S3 (book_id=%s)", book_id)
        return None
    return key


def upload_text(text: str, book_id: int, name: str) -> str | None:
    """Загружает вспомогательный текстовый артефакт (постраничный текст и т.п.)."""
    if not is_configured():
        return None
    key = f"sources/{book_id}/{name}"
    try:
        _client().put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
    except Exception:
        logger.exception("Не удалось загрузить текстовый артефакт в S3 (book_id=%s)", book_id)
        return None
    return key


def presigned_url(key: str, expires_seconds: int = 3600) -> str | None:
    """Временная ссылка на объект — для просмотра оригинала из админки/Source Viewer."""
    if not is_configured():
        return None
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": key},
            ExpiresIn=expires_seconds,
        )
    except Exception:
        logger.exception("Не удалось создать presigned URL для %s", key)
        return None
