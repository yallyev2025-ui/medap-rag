"""SQLAlchemy модели: Book, BookChunk (фаза 01), User, Usage, Query (фаза 04),
AIUsageEvent, IngestJob, AuditLog (этап 1 MedAP Student AI)."""

from datetime import date as date_, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings
from constants import SOURCE_TEXTBOOK


class Base(DeclarativeBase):
    pass


class Book(Base):
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
