"""SQLAlchemy модели: Book, BookChunk (фаза 01), User, Usage, Query (фаза 04),
AIUsageEvent, IngestJob, AuditLog (этап 1 MedAP Student AI)."""

from datetime import date as date_, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings
from constants import (
    DEFAULT_AUTHORITY_LEVEL,
    DEFAULT_SOURCE_STATUS,
    DEFAULT_VERIFICATION_STATUS,
    SOURCE_TEXTBOOK,
)


class Base(DeclarativeBase):
    pass


class Book(Base):
    """Источник (учебник/методичка/клинрек). Он же "Source" из §8/§9 ТЗ — имя
    таблицы не переименовываем, чтобы не ломать существующие FK/данные."""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    # Тип источника: 'учебник' или 'клинрек'. По умолчанию учебник — существующие
    # книги, загруженные до v2, автоматически считаются учебниками.
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_TEXTBOOK)
    chunks_count: Mapped[int] = mapped_column(Integer, default=0)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # --- Метаданные и provenance (§8–§9 ТЗ, этап 2) -----------------------------
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    edition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # См. constants.AUTHORITY_LEVELS — приоритет источника при отборе evidence
    # (этап 3), не подменяет фильтры retrieval здесь.
    authority_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DEFAULT_AUTHORITY_LEVEL
    )
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DEFAULT_VERIFICATION_STATUS
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    # draft/production/disabled/archived — см. constants.SOURCE_STATUSES.
    # "disabled"/"archived" исключают источник из retrieval без удаления чанков.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DEFAULT_SOURCE_STATUS)
    # Ключ оригинала в S3 (sources/{book_id}/{checksum}.{ext}); None — S3 не
    # сконфигурирован или загрузка оригинала не удалась (мягкая деградация —
    # чанки и эмбеддинги от этого не страдают).
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Ключ постраничного текста в S3 (для Source Viewer/переиндексации без OCR).
    pages_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reindexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BookChunk(Base):
    __tablename__ = "book_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    # Дублируем source_type в чанки, чтобы фильтровать поиск одним WHERE без JOIN.
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_TEXTBOOK)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.EMBEDDING_DIM), nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Денормализованные копии метаданных источника (§8 ТЗ, этап 2) ----------
    # Как и author/title/subject выше — копии для фильтрации/показа одним WHERE
    # без JOIN на books; синхронизируются в db.crud.update_book при переименовании
    # и при загрузке в scripts.load_books.load_book.
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Список Knowledge Unit id (продуктовый backend, вне этого репозитория) —
    # JSON-массив строк в Text; поле зарезервировано по §8, не заполняется пока
    # ничем в V1 текущего репозитория.
    knowledge_unit_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    edition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authority_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DEFAULT_AUTHORITY_LEVEL
    )
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DEFAULT_VERIFICATION_STATUS
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    # Приватные материалы (этап 4, §18) — зарезервировано по §8, пока не используется.
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exam_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Смещения фрагмента внутри raw-текста страницы page_from — best-effort
    # (заполняется, когда чанк целиком лежит на одной странице; None для
    # чанков, растянутых на несколько страниц). Нужны для подсветки конкретного
    # места в учебнике на этапе 3 (Source Viewer).
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # BM25 (§7 ТЗ): GENERATED ALWAYS AS ... STORED-колонка. Computed() здесь
    # даёт то же выражение, что и идемпотентный ALTER в db/init_db.py (нужный
    # для уже существующих в проде таблиц) — на чистой БД create_all создаёт
    # колонку сразу генерируемой, а не обычной, которую потом никто не заполнит.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', content)", persisted=True), nullable=True
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Выбранный режим работы (source_type: 'учебник'/'клинрек') и предмет/категория.
    # None у обоих — режим ещё не выбран, показываем главное меню. У клинреков
    # current_subject=None при source_type='клинрек' означает «Все категории».
    current_source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Режим «Разбор по симптомам» (клинреки): каждый вопрос трактуется как дифдиагноз.
    clinrek_symptom_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    # Начало текущего «чата»: лёгкая память диалога берётся только с этого момента.
    # Кнопка/команда «Новая тема» сдвигает границу — прошлые сообщения забываются.
    chat_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_usage_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0)


class AIUsageEvent(Base):
    """Расход на один вызов LLM-провайдера (§59 ТЗ).

    Пишется на КАЖДЫЙ вызов: без этой таблицы нельзя посчитать стоимость на
    активного пользователя, перцентили и прогноз месячного расхода, а ТЗ требует
    usage metering обязательной частью V1 (§44).
    """

    __tablename__ = "ai_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Глобальный user_id MedAP либо telegram:<id>; None — служебный вызов из админки.
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    image_units: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # Стоимость провайдера в долларах и её рублёвый эквивалент на момент вызова.
    provider_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    provider_cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    pricing_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Провайдер, который должен был отвечать по TaskModelMap, если сработал фолбэк.
    fallback_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    retrieval_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class IngestJob(Base):
    """Состояние загрузки источника через админку.

    Конвейер по разделу 3 дополнения к ТЗ: Upload → Parsing → ... → Ready. Статус
    живёт в БД, а не в памяти процесса, чтобы прогресс переживал рестарт контейнера
    (на App Platform файловая система эфемерная).
    """

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_TEXTBOOK)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # pending → running → done | error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # Текущая стадия конвейера для отображения в админке.
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    chunks_count: Mapped[int] = mapped_column(Integer, default=0)
    book_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Журнал критичных операций (§33 ТЗ): вход в админку, загрузка и удаление
    источника, смена маппинга моделей."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
