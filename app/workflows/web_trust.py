"""Отбор источников интернета по достоверности (ТЗ 20.11.2026).

ЗАЧЕМ. Поиск отдаёт десятки ссылок, среди которых стоматология за углом,
«здоровье и красота» и SEO-помойки. Показывать их на слайде нельзя: источник
должен быть таким, чтобы его не стыдно было открыть на защите.

ПРАВИЛО ВЛАДЕЛЬЦА: не жёсткий белый список. Ядро доверенных сайтов всегда
проходит; остальные оцениваются по признакам — домен, зона, глубина адреса.
Иностранные сайты разрешены: .edu, .gov, крупные общества и журналы.

Модуль чистый: никакой сети, только строки. Так его можно проверить тестом.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Ядро: этим верим всегда (медицина и официальные органы)
TRUSTED_DOMAINS = (
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "who.int",
    "cdc.gov",
    "nih.gov",
    "nice.org.uk",
    "nhs.uk",
    "ema.europa.eu",
    "fda.gov",
    "cochrane.org",
    "minzdrav.gov.ru",
    "cr.minzdrav.gov.ru",
    "rospotrebnadzor.ru",
    "msdmanuals.com",
    "medscape.com",
    "heart.org",
    "escardio.org",
    "ersnet.org",
    "esmo.org",
    "diabetes.org",
    "cancer.gov",
    "euro.who.int",
)

# Признаки мусора: клиники, красота, магазины, форумы, подборы
JUNK_WORDS = (
    "clinic",
    "klinik",
    "dental",
    "stomatolog",
    "zub",
    "beauty",
    "krasota",
    "cosmetolog",
    "plastika",
    "forum",
    "otzyv",
    "market",
    "wildberries",
    "ozon",
    "avito",
    "yandex.market",
)

GOOD_TLDS = (".gov", ".edu", ".ac.uk", ".edu.au", ".gov.uk", ".who.int")


def domain_of(url: str) -> str:
    """Домен без www; пустая строка, если адрес не разобрать."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _is_trusted(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in TRUSTED_DOMAINS)


def _is_junk(host: str, title: str = "") -> bool:
    text = f"{host} {title}".lower()
    return any(word in text for word in JUNK_WORDS)


def trust_score(url: str, title: str = "") -> int:
    """Оценка достоверности адреса. Чем выше — тем охотнее показываем."""
    host = domain_of(url)
    if not host or "." not in host:
        return -10

    score = 0
    if _is_trusted(host):
        score += 5
    if host.endswith(GOOD_TLDS) or host.endswith(".org") and host.count(".") >= 1:
        score += 2
    if url.startswith("https://"):
        score += 1
    if _is_junk(host, title):
        score -= 6
    # Длинные дефисные домены — типичный признак SEO-помоек
    if host.count("-") >= 3:
        score -= 2
    # Глубокая страница доверенного сайта — это нормально; у случайного
    # сайта глубина ничего не добавляет и не отнимает
    return score


def rank_results(results: list[dict], *, limit: int) -> list[dict]:
    """Оставить только достоверные ссылки, лучшие — первыми.

    Ничего не «додумываем»: если после отбора пусто, пусто и вернётся —
    вызывающий решает, писать по общим знаниям или честно сказать.
    """
    scored: list[tuple[int, int, dict]] = []
    seen: set[str] = set()
    for index, item in enumerate(results):
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        host = domain_of(url)
        if not host or url in seen:
            continue
        seen.add(url)
        score = trust_score(url, title)
        if score < 1:
            continue
        scored.append((score, index, item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in scored[:limit]]


def parse_searxng_results(payload: dict) -> list[dict]:
    """Ответ SearXNG → единый вид {url, title, content}. Чистая функция."""
    results: list[dict] = []
    for row in (payload or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "url": url,
                "title": str(row.get("title") or "").strip(),
                "content": str(row.get("content") or "").strip(),
            }
        )
    return results
