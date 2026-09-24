"""Извлечение текста из учебников (PDF/Word/txt) с OCR сканов, отсев служебных
страниц и чанкинг по предложениям."""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pdfplumber

from config import settings

logger = logging.getLogger(__name__)

# ВАЖНО: не импортируем rag.embedder (и через него torch) на уровне модуля.
# Извлечение текста из PDF (extract_document) в параллельных процессах-воркерах
# импортирует этот модуль, но модель ему не нужна — иначе каждый воркер тянул бы
# ~1 ГБ torch в память. _get_model загружаем лениво, только где реально нужен.

# OCR для сканированных PDF (страницы-картинки без текстового слоя). Зависимости
# системные (tesseract + poppler) — если их нет (локально), мягко деградируем:
# текстовые PDF/docx/txt всё равно работают, а сканы просто не распознаются.
try:
    import pytesseract
    from pdf2image import convert_from_path

    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

# python-docx для .docx
try:
    import docx as _docx

    _DOCX_AVAILABLE = True
except ImportError:
    _DOCX_AVAILABLE = False

MIN_CHUNK_TOKENS = 20

# Жёсткое окно модели-эмбеддера. Текст длиннее модель обрезала бы при кодировании,
# и «хвост» фрагмента не попал бы в поисковый отпечаток. Поэтому гарантируем, что
# КАЖДЫЙ чанк целиком помещается в это окно — ничего не теряется из индекса.
DEFAULT_MODEL_MAX_TOKENS = 512
# Запас под префикс "passage: ", служебные токены (CLS/SEP) и возможный дрейф
# токенизации при склейке предложений.
MODEL_TOKEN_RESERVE = 24

# Ниже этого числа символов на странице считаем, что текстового слоя нет (скан) —
# и пробуем распознать страницу через OCR.
OCR_MIN_CHARS = 20
OCR_LANG = "rus+eng"
OCR_DPI = 300

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt")

# Точечные лидеры оглавления: "Тема ........ 42"
_DOTTED_LEADER = re.compile(r"\.\s?\.\s?\.\s?\.")
# Строка вида "...текст... 123" (заканчивается номером страницы)
_LINE_ENDS_WITH_PAGENO = re.compile(r"\S\s+\d{1,4}\s*$")
# Разбивка на предложения: после .!?… и пробела.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Эвристики заголовка раздела (структурный чанкинг, §8 ТЗ): нумерованный
# заголовок вида "12.3 Тема" или "Глава 5 ...", либо короткая строка целиком
# заглавными буквами — частые паттерны в учебниках. Настоящий текст почти
# всегда заканчивается пунктуацией, заголовок — почти никогда.
_HEADING_NUMBERED = re.compile(r"^\s*\d{1,2}(\.\d{1,2}){0,3}\.?\s+\S")
_HEADING_CHAPTER = re.compile(r"^\s*(глава|раздел|тема|часть)\s+\d", re.IGNORECASE)
_HEADING_MAX_WORDS = 12
_HEADING_MAX_CHARS = 90


@dataclass
class Chunk:
    content: str
    # None — если у формата нет осмысленных номеров страниц (docx, txt без разметки).
    page_from: int | None
    page_to: int | None
    # Заголовок раздела, под которым лежит начало чанка (эвристика, см.
    # _is_heading_line) — структурный чанкинг по §8 ТЗ, этап 2. None, если
    # эвристика ничего не нашла до этого места в документе.
    section: str | None = None
    # Смещения фрагмента в исходном (не нормализованном) тексте страницы
    # page_from — best-effort, только для чанков целиком на одной странице
    # (см. _add_char_offsets). Нужны для подсветки места ответа на этапе 3.
    char_start: int | None = None
    char_end: int | None = None


# Сколько подряд идущих страниц рендерим одним вызовом convert_from_path. Батч
# нужен, чтобы poppler не перепарсивал весь PDF с нуля на каждую страницу (см.
# _ocr_pdf_pages) — но не рендерим весь документ разом, чтобы не раздувать пик
# памяти на сотнях страниц, когда в процессе уже живут эмбеддер и реранкер.
OCR_BATCH_SIZE = 20


def _contiguous_runs(numbers: list[int]) -> list[list[int]]:
    """Группирует отсортированный список номеров страниц в подряд идущие серии."""
    runs: list[list[int]] = []
    for n in numbers:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return runs


def _ocr_pdf_pages(pdf_path: str, page_numbers: list[int]) -> dict[int, str]:
    """Распознаёт страницы PDF (1-based номера), батчами по OCR_BATCH_SIZE подряд
    идущих страниц за один вызов convert_from_path.

    Раньше вызов был один на КАЖДУЮ страницу отдельно (first_page=last_page=N) —
    poppler при этом заново открывает и парсит весь PDF на каждый вызов, так что
    стоимость растёт ~O(n²) от числа страниц-сканов: на учебнике в сотни страниц
    это выливается в минуты (иногда фактическое зависание) вместо секунд.

    Внутри батча рендер и распознавание идут параллельно (settings.OCR_WORKERS):
    рендер — через встроенный `thread_count` pdf2image/poppler, распознавание —
    через ThreadPoolExecutor, т.к. pytesseract каждый вызов шеллит внешний процесс
    tesseract и реально ждёт на I/O, а не держит GIL, поэтому потоки дают
    настоящий параллелизм по ядрам CPU. DPI не трогаем — качество то же самое,
    ускоряется только то, сколько страниц обрабатывается одновременно.
    """
    results: dict[int, str] = {}
    for run in _contiguous_runs(page_numbers):
        for start in range(0, len(run), OCR_BATCH_SIZE):
            batch = run[start : start + OCR_BATCH_SIZE]
            images = convert_from_path(
                pdf_path,
                dpi=OCR_DPI,
                first_page=batch[0],
                last_page=batch[-1],
                thread_count=settings.OCR_WORKERS,
            )
            with ThreadPoolExecutor(max_workers=settings.OCR_WORKERS) as pool:
                recognized = pool.map(lambda img: pytesseract.image_to_string(img, lang=OCR_LANG), images)
                for page_number, text in zip(batch, recognized):
                    results[page_number] = text
    return results


def extract_pages(pdf_path: str) -> list[str]:
    """Текст PDF по страницам. Страницы без текстового слоя (сканы) добиваются OCR."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]

    if not _OCR_AVAILABLE:
        return pages

    needs_ocr = [i + 1 for i, text in enumerate(pages) if len(text.strip()) < OCR_MIN_CHARS]
    if not needs_ocr:
        return pages

    try:
        recognized_by_page = _ocr_pdf_pages(pdf_path, needs_ocr)
    except Exception:
        # Системный OCR (tesseract/poppler) недоступен — тихо деградируем без
        # спама трейсбеков (текстовые PDF от этого не страдают, а сканы просто
        # не распознаются).
        logger.warning("Системный OCR недоступен — страницы-сканы этого PDF пропущены")
        return pages

    for page_number, recognized in recognized_by_page.items():
        i = page_number - 1
        if len(recognized.strip()) > len(pages[i].strip()):
            pages[i] = recognized

    return pages


def extract_docx(docx_path: str) -> str:
    document = _docx.Document(docx_path)
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def extract_txt(txt_path: str) -> str:
    with open(txt_path, encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_document(path: str) -> tuple[list[str], bool]:
    """Извлекает текст из файла учебника по расширению.

    Возвращает (страницы, paged), где paged=True означает, что номера страниц
    осмысленны (PDF; txt-sidecar от ocrmypdf с разделителем страниц \\f).
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return extract_pages(path), True

    if ext == ".txt":
        raw = extract_txt(path)
        # ocrmypdf --sidecar разделяет страницы символом перевода страницы \f —
        # тогда номера страниц восстановимы.
        if "\f" in raw:
            return raw.split("\f"), True
        return [raw], False

    if ext == ".docx":
        if not _DOCX_AVAILABLE:
            raise ValueError("Поддержка .docx недоступна: не установлен python-docx")
        return [extract_docx(path)], False

    raise ValueError(f"Неподдерживаемый формат файла: {ext}")


def is_service_page(page_text: str) -> bool:
    """Эвристика: страница оглавления/индекса/списка (а не связный учебный текст).

    Такие страницы плотно совпадают с короткими запросами-терминами в эмбеддинг-
    пространстве и систематически «протаскиваются» в retrieval, провоцируя выдумки.
    Отсеиваем их до чанкинга.
    """
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False

    dotted = sum(1 for ln in lines if _DOTTED_LEADER.search(ln))
    if dotted / len(lines) >= 0.3:
        return True

    ends_with_page = sum(1 for ln in lines if _LINE_ENDS_WITH_PAGENO.search(ln))
    if len(lines) >= 6 and ends_with_page / len(lines) >= 0.5:
        return True

    return False


def _is_heading_line(line: str) -> bool:
    """Эвристика заголовка раздела — короткая строка без пунктуации на конце,
    совпадающая с типичным паттерном заголовка учебника."""
    if not line or len(line) > _HEADING_MAX_CHARS:
        return False
    if line[-1] in ".!?…,:;":
        return False
    if _HEADING_CHAPTER.match(line):
        return True
    words = line.split()
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    if _HEADING_NUMBERED.match(line):
        return True
    letters = [ch for ch in line if ch.isalpha()]
    # Строка целиком заглавными буквами (без учёта цифр/пунктуации) — второй
    # частый паттерн заголовка раздела.
    return bool(letters) and all(ch.isupper() for ch in letters)


def _page_sentences(page_text: str) -> list[tuple[str, str | None]]:
    """Склеивает визуальные переносы строк PDF в связный текст, режет на
    предложения и параллельно отслеживает текущий раздел по эвристике
    заголовка — возвращает (предложение, заголовок_раздела_или_None)."""
    lines = page_text.splitlines()
    current_section: str | None = None
    paragraph: list[str] = []
    out: list[tuple[str, str | None]] = []

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            normalized = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
            for sentence in _SENTENCE_SPLIT.split(normalized):
                sentence = sentence.strip()
                if sentence:
                    out.append((sentence, current_section))
        paragraph = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if _is_heading_line(line):
            flush()
            current_section = line
            continue
        paragraph.append(line)
    flush()
    return out


def _hard_split(text: str, page: int | None, section: str | None, chunk_size: int, tokenizer) -> list[Chunk]:
    """Дробит сверхдлинное предложение (таблица, формула без пунктуации) по токенам."""
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    out = []
    for start in range(0, len(token_ids), chunk_size):
        piece = token_ids[start : start + chunk_size]
        if len(piece) < MIN_CHUNK_TOKENS:
            break
        out.append(Chunk(content=tokenizer.decode(piece), page_from=page, page_to=page, section=section))
    return out


def _add_char_offsets(chunks: list[Chunk], pages: list[str]) -> None:
    """Best-effort смещения фрагмента в исходном тексте страницы (§8 ТЗ, нужны
    для подсветки на этапе 3) — только для чанков целиком на одной странице:
    более широкий (кросс-страничный) чанк не имеет единого "исходного текста",
    в котором его искать, и остаётся с char_start/char_end = None."""
    normalized_pages = [re.sub(r"\s+", " ", p) for p in pages]
    for chunk in chunks:
        if chunk.page_from is None or chunk.page_from != chunk.page_to:
            continue
        if not (1 <= chunk.page_from <= len(normalized_pages)):
            continue
        idx = normalized_pages[chunk.page_from - 1].find(chunk.content)
        if idx >= 0:
            chunk.char_start = idx
            chunk.char_end = idx + len(chunk.content)


def chunk_text(
    pages: list[str],
    chunk_size: int = settings.CHUNK_SIZE_TOKENS,
    overlap: int = settings.CHUNK_OVERLAP_TOKENS,
    paged: bool = True,
) -> list[Chunk]:
    """Режет текст на чанки. paged=False — у формата нет номеров страниц
    (docx, txt без разметки), у чанков page_from/page_to будут None."""
    from rag.embedder import _get_model  # ленивый импорт: torch грузится только здесь

    tokenizer = _get_model().tokenizer

    # (предложение, номер страницы, число токенов, раздел) по всем НЕслужебным
    # страницам. Раздел — эвристика по заголовкам (_page_sentences), пробрасывается
    # в Chunk.section для структурного чанкинга (§8 ТЗ).
    sentences: list[tuple[str, int, int, str | None]] = []
    for page_number, page_text in enumerate(pages, start=1):
        # Фильтр служебных страниц (оглавление и т.п.) уместен только для постраничных
        # форматов; для единого текстового блока (docx/txt) пропустить его, чтобы
        # случайно не отбросить весь учебник.
        if paged and is_service_page(page_text):
            continue
        for sentence, section in _page_sentences(page_text):
            token_len = len(tokenizer.encode(sentence, add_special_tokens=False))
            sentences.append((sentence, page_number, token_len, section))

    chunks: list[Chunk] = []
    current: list[tuple[str, int, int, str | None]] = []

    def emit() -> None:
        if not current or sum(s[2] for s in current) < MIN_CHUNK_TOKENS:
            return
        pages_in = [s[1] for s in current]
        # Раздел чанка — по первому предложению: ближе к тому, "о чём" начинается
        # фрагмент, чем усреднение по всем разделам, которые он может захватывать.
        chunks.append(
            Chunk(
                content=" ".join(s[0] for s in current),
                page_from=min(pages_in) if paged else None,
                page_to=max(pages_in) if paged else None,
                section=current[0][3],
            )
        )

    def overlap_tail() -> list[tuple[str, int, int, str | None]]:
        """Последние предложения текущего чанка в пределах overlap токенов — для переноса."""
        tail: list[tuple[str, int, int, str | None]] = []
        tail_tokens = 0
        for sentence in reversed(current):
            if tail_tokens + sentence[2] > overlap:
                break
            tail.insert(0, sentence)
            tail_tokens += sentence[2]
        return tail

    for sentence, page_number, token_len, section in sentences:
        item = (sentence, page_number, token_len, section)

        if token_len > chunk_size:
            emit()
            current = []
            chunks.extend(
                _hard_split(sentence, page_number if paged else None, section, chunk_size, tokenizer)
            )
            continue

        current_tokens = sum(s[2] for s in current)
        if current and current_tokens + token_len > chunk_size:
            emit()
            current = overlap_tail()

        current.append(item)

    emit()
    result = _enforce_model_window(chunks, tokenizer)
    _add_char_offsets(result, pages)
    return result


def _enforce_model_window(chunks: list[Chunk], tokenizer) -> list[Chunk]:
    """Гарантия против обрезки: каждый чанк при кодировании должен целиком влезать
    в окно модели-эмбеддера. Эмбеддер считает вектор только по первым max_seq
    токенам, поэтому фрагменты длиннее дорезаем по фактическим токенам — так ни один
    кусок текста не остаётся вне поискового индекса (критично для клинреков)."""
    from rag.embedder import _get_model  # ленивый импорт: torch грузится только здесь

    model = _get_model()
    max_tokens = getattr(model, "max_seq_length", None) or DEFAULT_MODEL_MAX_TOKENS
    budget = max_tokens - MODEL_TOKEN_RESERVE

    safe: list[Chunk] = []
    for chunk in chunks:
        # Длину считаем ровно так, как её увидит эмбеддер: с префиксом "passage: "
        # и служебными токенами. Сравниваем с budget (окно минус запас), а не с
        # самим окном: внутренняя токенизация модели может дать на пару токенов
        # больше, чем этот замер, — запас гарантирует, что даже с дрейфом влезем.
        encoded = tokenizer.encode("passage: " + chunk.content, add_special_tokens=True)
        if len(encoded) <= budget:
            safe.append(chunk)
            continue
        body_ids = tokenizer.encode(chunk.content, add_special_tokens=False)
        for start in range(0, len(body_ids), budget):
            piece = body_ids[start : start + budget]
            if len(piece) < MIN_CHUNK_TOKENS:
                break
            safe.append(
                Chunk(
                    content=tokenizer.decode(piece),
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    section=chunk.section,
                )
            )
    return safe

