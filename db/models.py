"""SQLAlchemy модели: Book, BookChunk (фаза 01), User, Usage, Query (фаза 04)."""

from datetime import date as date_, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
