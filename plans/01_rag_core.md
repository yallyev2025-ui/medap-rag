# Фаза 01 — RAG-ядро (эмбеддинги, чанкинг, pgvector retriever, загрузка книг)

## Цель
Реализовать пайплайн обработки учебников и поиск релевантных фрагментов через pgvector — полностью независимо от Telegram-бота, тестируется CLI-скриптом.

## Предпосылки
Фаза 00 выполнена: есть `config.py`, зависимости установлены.

## Что создать

### `db/models.py`
SQLAlchemy модель `BookChunk`:
- `id` (PK)
- `book_id` (FK на `books`, таблица создаётся в фазе 04 — здесь временно просто `Integer`, без ForeignKey constraint, либо создать минимальную таблицу `books` уже здесь, т.к. чанки на неё ссылаются)
- `subject: str` (pathanatomy, pathphys, physiology, anatomy, biochemistry, pharmacology, ...)
- `author: str`
- `title: str`
- `chunk_index: int`
- `content: text`
- `embedding: Vector(1024)` (через `pgvector.sqlalchemy.Vector`)

Создать также минимальную таблицу `books` (title, author, subject, chunks_count, loaded_at) — полноценно используется в фазе 04/05, но нужна как FK-таргет уже здесь.

Скрипт инициализации БД: `db/init_db.py` — выполняет `CREATE EXTENSION IF NOT EXISTS vector;` и `Base.metadata.create_all()`.

### `rag/embedder.py`
- Загружает `SentenceTransformer(settings.EMBEDDING_MODEL_NAME)` один раз (ленивая инициализация — модуль-level singleton, чтобы не грузить модель при каждом импорте без необходимости).
- `embed_passages(texts: list[str]) -> list[list[float]]` — добавляет префикс `"passage: "` к каждому тексту перед encode (требование e5-моделей), `normalize_embeddings=True`.
- `embed_query(text: str) -> list[float]` — добавляет префикс `"query: "`.
- Обе функции возвращают обычные python-списки floats (для записи в pgvector).

### `rag/processor.py`
- `extract_text(pdf_path: str) -> str` — через pdfplumber, постранично, склейка с `\n`.
- `chunk_text(text: str, chunk_size=512, overlap=50) -> list[str]` — токенизация через tokenizer модели эмбеддера (`SentenceTransformer.tokenizer`) или простой word-based подход с приблизительным соответствием токенов; разбивает на чанки по `chunk_size` токенов с overlap `overlap` токенов между соседними чанками.
- Обработка пустых/слишком коротких чанков — отбрасывать чанки короче ~20 токенов.

### `rag/retriever.py`
- `async def retrieve(question: str, top_k: int = 5) -> list[ChunkResult]` где `ChunkResult` содержит `content, subject, author, title, distance`.
- Реализация: `embed_query(question)` → SQL через SQLAlchemy с pgvector оператором `<=>` (cosine distance):
```sql
SELECT content, subject, author, title,
       embedding <=> :query_embedding AS distance
FROM book_chunks
ORDER BY distance
LIMIT :top_k
```
- Поиск идёт по всем subjects сразу (без фильтра) — согласно ТЗ "по всем коллекциям всех предметов".
- Возвращает также `distance` — используется в фазе 02 для порога "нет релевантного контекста".

### `scripts/load_books.py`
CLI на `argparse`:
```
python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy --author "Струков" --title "Патологическая анатомия"
```
Логика:
1. `extract_text` → `chunk_text`.
2. Создать запись в `books` (title, author, subject, chunks_count=len(chunks), loaded_at=now).
3. `embed_passages(chunks)` — батчами (например по 32), чтобы не упереться в память.
4. Записать каждый чанк в `book_chunks` с `book_id`, `chunk_index`, `embedding`.
5. Вывести в stdout прогресс (количество обработанных чанков).
6. **Не удалять PDF автоматически** в этой фазе (удаление — забота пользователя/админ-команды в фазе 05), но залогировать "можно удалить исходный PDF: {path}".

### Тест фазы
1. `python -m db.init_db` — создаёт расширение и таблицы без ошибок.
2. Взять тестовый PDF (текстовый, можно небольшой учебный материал на 2-3 страницы) → `python -m scripts.load_books --pdf test.pdf --subject physiology --author "Test" --title "Test Book"`.
3. Написать `scripts/test_retrieve.py` (или интерактивно через `python -c`) — вызвать `retrieve("вопрос по теме из тестового PDF")`, убедиться, что возвращаются релевантные чанки именно из этой книги (низкий `distance`).

## Зависимость для следующих фаз
- `rag/embedder.py`, `rag/retriever.py` используются в фазе 02/03.
- `db/models.py` (BookChunk, Book) расширяется в фазе 04 таблицами users/usage/queries.
