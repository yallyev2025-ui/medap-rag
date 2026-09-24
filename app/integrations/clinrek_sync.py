"""Синхронизация клинических рекомендаций с Рубрикатором Минздрава РФ
(cr.minzdrav.gov.ru, батч 9) — проверка новых версий и загрузка по кнопке в
админке, НЕ полностью автоматически по расписанию (решение пользователя).

Закрытый JSON/PDF API на apicr.minzdrav.gov.ru/api.ashx: список действующих
рекомендаций (GetJsonClinrecsFilterV2), прямая публичная PDF-ссылка без
авторизации (GetClinrecPdf&id={код}_{версия}). Подход подтверждён сторонним
опенсорсным проектом (SEnikeeva/rag-clinical-prototype) — обычные HTTP-запросы,
без браузера.

ЧЕСТНО НЕИЗВЕСТНО (нельзя проверить из песочницы разработки — домен заблокирован
политикой окружения, у пользователя сайт тоже не грузится): точные имена полей в
ответе GetJsonClinrecsFilterV2. Парсинг ниже защищённый — если ожидаемых ключей
нет, логируется предупреждение со списком реально пришедших ключей и запись
пропускается, а не выдумывается. Первый реальный запуск на Timeweb может
потребовать небольшой правки списка кандидатов полей.
"""

import logging
import os
import tempfile
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from constants import CLINREK_CATEGORIES, SOURCE_CLINREK
from db.models import Book
from scripts.load_books import load_book

logger = logging.getLogger(__name__)

CLINREK_API_URL = "https://apicr.minzdrav.gov.ru/api.ashx"
_TIMEOUT = 20.0

# Кандидаты имён полей в ответе GetJsonClinrecsFilterV2 — реальные не подтверждены
# живым запросом, пробуем по очереди, самый вероятный вариант первым.
_CODE_FIELDS = ("id", "code", "rubricId", "ID")
_VERSION_FIELDS = ("version", "ver", "versionNumber")
_TITLE_FIELDS = ("title", "name", "rubricName")
_CATEGORY_FIELDS = ("category", "ageCategory", "patientCategory")


@dataclass
class RemoteClinrec:
    external_ref: str  # "{код}_{версия}"
    code: str
    version: str
    title: str
    category: str | None  # как пришло с сайта, ещё не смаплено на CLINREK_CATEGORIES


@dataclass
class ClinrecUpdate:
    remote: RemoteClinrec
    is_new: bool
    previous_book_id: int | None = None


def _first_present(item: dict, fields: tuple[str, ...]) -> str | None:
    for field_name in fields:
        value = item.get(field_name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _map_category(raw_category: str | None) -> str | None:
    """Категория с сайта -> значение subject из CLINREK_CATEGORIES. Незнакомое
    значение — не роняем синхронизацию, просто оставляем subject пустым (админ
    поправит вручную при загрузке), а не угадываем."""
    if not raw_category:
        return None
    lowered = raw_category.lower()
    for _code, _label, value in CLINREK_CATEGORIES:
        if value and value in lowered:
            return value
    if "взросл" in lowered or "adult" in lowered:
        return "взрослые"
    if "дет" in lowered or "child" in lowered:
        return "дети"
    return None


async def list_remote_recommendations() -> list[RemoteClinrec]:
    """Список действующих рекомендаций с сайта. Записи с неожидаемой структурой
    пропускаются с предупреждением в лог, а не молча искажаются."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(CLINREK_API_URL, params={"op": "GetJsonClinrecsFilterV2"})
        response.raise_for_status()
        data = response.json()

    items = data if isinstance(data, list) else data.get("items") or data.get("data") or []
    results: list[RemoteClinrec] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _first_present(item, _CODE_FIELDS)
        version = _first_present(item, _VERSION_FIELDS)
        title = _first_present(item, _TITLE_FIELDS)
        if not code or not version or not title:
            logger.warning(
                "Clinrek sync: запись без ожидаемых полей (code/version/title), пропущена. "
                "Реальные ключи: %s", list(item.keys()),
            )
            continue
        category = _first_present(item, _CATEGORY_FIELDS)
        results.append(
            RemoteClinrec(
                external_ref=f"{code}_{version}",
                code=code,
                version=version,
                title=title,
                category=category,
            )
        )
    return results


async def check_for_updates(session: AsyncSession) -> list[ClinrecUpdate]:
    """Сравнивает список с сайта с уже загруженными Book.external_ref у клинреков.
    Новый код -> is_new=True; тот же код, но версия выше -> previous_book_id
    заполнен; совпадает целиком -> не попадает в список (нечего обновлять)."""
    remote_list = await list_remote_recommendations()

    stmt = select(Book).where(
        Book.source_type == SOURCE_CLINREK,
        Book.external_ref.is_not(None),
    )
    existing_books = (await session.execute(stmt)).scalars().all()
    # code -> (version, book_id) — берём максимальную версию, если по ошибке
    # оказалось несколько записей с одним кодом.
    by_code: dict[str, tuple[str, int]] = {}
    for book in existing_books:
        code, _, version = book.external_ref.rpartition("_")
        if not code:
            continue
        current = by_code.get(code)
        if current is None or version > current[0]:
            by_code[code] = (version, book.id)

    updates: list[ClinrecUpdate] = []
    for remote in remote_list:
        current = by_code.get(remote.code)
        if current is None:
            updates.append(ClinrecUpdate(remote=remote, is_new=True))
        elif remote.version > current[0]:
            updates.append(ClinrecUpdate(remote=remote, is_new=False, previous_book_id=current[1]))
        # remote.version <= current[0] -> ничего не меняем, не в списке.
    return updates


async def fetch_pdf(external_ref: str) -> bytes:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            CLINREK_API_URL, params={"id": external_ref, "op": "GetClinrecPdf"}
        )
        response.raise_for_status()
        return response.content


async def apply_update(session: AsyncSession, update: ClinrecUpdate) -> int:
    """Скачивает PDF и грузит его тем же конвейером, что и ручная загрузка
    (load_book) — extract/chunk/embed/S3 не дублируются. Старая версия (если
    была) архивируется, а не удаляется — история и откат сохраняются."""
    pdf_bytes = await fetch_pdf(update.remote.external_ref)

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(pdf_bytes)

        subject = _map_category(update.remote.category) or "взрослые_и_дети"
        book_id, _chunks_count = await load_book(
            tmp_path,
            subject=subject,
            author="Минздрав России",
            title=update.remote.title,
            source_type=SOURCE_CLINREK,
            authority_level="clinical_guideline",
        )
    finally:
        os.unlink(tmp_path)

    new_book = await session.get(Book, book_id)
    if new_book is not None:
        new_book.external_ref = update.remote.external_ref

    if update.previous_book_id is not None:
        old_book = await session.get(Book, update.previous_book_id)
        if old_book is not None:
            old_book.status = "archived"

    await session.commit()
    return book_id
