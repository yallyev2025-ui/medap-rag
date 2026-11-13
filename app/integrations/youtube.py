"""Субтитры YouTube-видео (13.11.2026, владелец: «пусть вытаскивает субтитры или речь оттуда, Виспер не нужен»).

Модель здесь НЕ вызывается и звук НЕ скачивается: только готовый текст, который YouTube сам отдаёт к видео (ручные или автоматические
субтитры) — библиотека youtube-transcript-api. Запускается на боте, потому что бот стоит за границей: с российского сервера сайта YouTube
текст не отдаёт. Дальше конспект по этому тексту пишет сайт (его промпт, его проверка).

Предпочтение языка: русские (ручные → автоматические) → английские → любые. Нет субтитров вовсе — честная ошибка `no_captions`, а не выдумка.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from app.observability.context import current_request_id

logger = logging.getLogger(__name__)

# Потолок: видео длиннее четырёх часов не берём вовсе (решение о более узком пределе принимает сайт)
MAX_DURATION_SEC = 4 * 3600
# Страховка по объёму текста (около четырёх часов речи — 250–300 тыс. знаков)
MAX_CHARS = 400_000
# Метка времени ставится не чаще, чем раз в столько секунд: по ней видно, где в видео то или иное место
MARK_EVERY_SEC = 60

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
_NOISE_RE = re.compile(r"\[(?:music|applause|laughter|музыка|аплодисменты|смех|звуки?[^\]]*)\]", re.IGNORECASE)


def parse_video_id(url: str) -> str | None:
    """Идентификатор видео из ссылки: watch?v=, youtu.be/, /shorts/, /embed/, /live/. Чужой адрес или не видео — `None`."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in _HOSTS:
        return None
    host = (u.hostname or "").lower()
    cand: str | None = None
    if host.endswith("youtu.be"):
        cand = u.path.lstrip("/").split("/")[0]
    elif u.path == "/watch":
        cand = (parse_qs(u.query).get("v") or [None])[0]
    else:
        m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", u.path)
        cand = m.group(1) if m else None
    return cand if cand and _ID_RE.match(cand) else None


@dataclass
class VideoText:
    video_id: str
    url: str
    title: str = ""
    author: str = ""
    language: str = ""
    is_generated: bool = False
    duration_sec: int = 0
    chars: int = 0
    text: str = ""
    truncated: bool = False
    error: str | None = None
    error_code: str | None = None
    request_id: str | None = None


def _mmss(sec: float) -> str:
    s = int(sec)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _clean(text: str) -> str:
    text = html.unescape(text).replace("\n", " ")
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"^>>\s*|\s>>\s", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_text(snippets) -> tuple[str, int]:
    """Текст с метками времени «[мм:сс]» раз в минуту; возвращает (текст, длительность в секундах)."""
    parts: list[str] = []
    last_mark = -MARK_EVERY_SEC
    end = 0.0
    for sn in snippets:
        line = _clean(sn.text)
        end = max(end, float(sn.start) + float(sn.duration))
        if not line:
            continue
        if float(sn.start) - last_mark >= MARK_EVERY_SEC:
            parts.append(f"\n[{_mmss(sn.start)}] ")
            last_mark = float(sn.start)
        parts.append(line + " ")
    return "".join(parts).strip(), int(end)


def _pick(transcripts):
    """Какие субтитры брать: русские ручные → русские автоматические → английские → любые (ручные раньше автоматических)."""
    items = list(transcripts)

    def rank(t) -> tuple[int, int]:
        lang = (t.language_code or "").lower()
        lang_rank = 0 if lang.startswith("ru") else 1 if lang.startswith("en") else 2
        return (lang_rank, 1 if t.is_generated else 0)

    return sorted(items, key=rank)[0] if items else None


def _fetch_sync(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi

    listing = YouTubeTranscriptApi().list(video_id)
    chosen = _pick(listing)
    if chosen is None:
        from youtube_transcript_api import NoTranscriptFound

        raise NoTranscriptFound(video_id, [], listing)
    return chosen, chosen.fetch()


def _classify(exc: Exception) -> tuple[str, str]:
    import youtube_transcript_api as y

    if isinstance(exc, (y.TranscriptsDisabled, y.NoTranscriptFound)):
        return "no_captions", "У этого видео нет субтитров, конспект сделать не из чего."
    if isinstance(exc, y.AgeRestricted):
        return "age", "Видео с возрастным ограничением: YouTube не отдаёт его субтитры без входа в аккаунт."
    if isinstance(exc, (y.VideoUnavailable, y.VideoUnplayable)):
        return "unavailable", "Видео недоступно: оно удалено, закрыто или недоступно в регионе."
    if isinstance(exc, y.InvalidVideoId):
        return "bad_url", "Не удалось понять ссылку на видео."
    if isinstance(exc, (y.IpBlocked, y.RequestBlocked, y.PoTokenRequired)):
        return "blocked", "YouTube сейчас не отдаёт субтитры с сервера. Попробуйте позже."
    return "failed", "Не удалось получить субтитры видео. Попробуйте позже."


async def _oembed(url: str) -> tuple[str, str]:
    """Название и канал через публичный oEmbed: нужны для подписи конспекта. Сбой не мешает — вернутся пустые строки."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            r = await client.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
        if r.status_code == 200:
            data = r.json()
            return str(data.get("title") or "")[:300], str(data.get("author_name") or "")[:200]
    except Exception:
        logger.info("YouTube oEmbed: название не получено", exc_info=True)
    return "", ""


async def fetch_video_text(url: str) -> VideoText:
    """Субтитры видео по ссылке. Никогда не бросает: ошибки приходят в `error` и `error_code`."""
    video_id = parse_video_id(url)
    rid = current_request_id()
    if not video_id:
        return VideoText(video_id="", url=url, error="Это не ссылка на видео YouTube.", error_code="bad_url", request_id=rid)
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    try:
        (chosen, fetched), (title, author) = await asyncio.gather(asyncio.to_thread(_fetch_sync, video_id), _oembed(canonical))
    except Exception as exc:  # noqa: BLE001 — все сбои библиотеки сводим к понятным кодам
        code, message = _classify(exc)
        logger.warning("YouTube: субтитры %s не получены (%s: %s)", video_id, type(exc).__name__, str(exc)[:200])
        return VideoText(video_id=video_id, url=canonical, error=message, error_code=code, request_id=rid)
    text, duration = build_text(fetched.snippets)
    if duration > MAX_DURATION_SEC:
        return VideoText(video_id=video_id, url=canonical, title=title, author=author, duration_sec=duration, error="Видео слишком длинное (больше четырёх часов).", error_code="too_long", request_id=rid)
    if not text:
        return VideoText(video_id=video_id, url=canonical, title=title, author=author, error="У видео пустые субтитры, конспект сделать не из чего.", error_code="no_captions", request_id=rid)
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    return VideoText(
        video_id=video_id, url=canonical, title=title, author=author, language=chosen.language_code, is_generated=bool(chosen.is_generated),
        duration_sec=duration, chars=len(text), text=text, truncated=truncated, request_id=rid,
    )
