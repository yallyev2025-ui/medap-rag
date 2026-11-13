"""Субтитры YouTube (13.11.2026): python -m scripts.test_youtube (без сети, без базы; библиотека подменяется)."""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.integrations import youtube as yt  # noqa: E402

checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"НЕ ПРОШЛО: {label}")


ID = "dQw4w9WgXcQ"
for url, want in [
    (f"https://www.youtube.com/watch?v={ID}&list=PL1&t=5s", ID),
    (f"https://youtu.be/{ID}?si=abc", ID),
    (f"https://m.youtube.com/shorts/{ID}", ID),
    (f"https://www.youtube.com/embed/{ID}", ID),
    (f"https://www.youtube.com/live/{ID}?feature=share", ID),
    ("https://evil.com/watch?v=" + ID, None),
    ("https://www.youtube.com/watch?v=short", None),
    ("https://www.youtube.com/playlist?list=PL1", None),
    ("ftp://youtube.com/watch?v=" + ID, None),
    ("не ссылка", None),
]:
    check(yt.parse_video_id(url) == want, f"разбор ссылки {url} → {yt.parse_video_id(url)}")

sn = lambda t, s, d: SimpleNamespace(text=t, start=s, duration=d)  # noqa: E731
text, dur = yt.build_text([sn("Добрый день,\n[Музыка]", 0, 3), sn("сегодня &gt;&gt; поговорим", 3, 3), sn(">> об инфаркте", 62, 4), sn("   ", 70, 1)])
check(text.startswith("[00:00] Добрый день, сегодня"), f"метка в начале: {text!r}")
check("[01:02] об инфаркте" in text, f"вторая метка после минуты: {text!r}")
check("Музыка" not in text and "&gt;" not in text, "шум и сущности убраны")
check(dur == 71, f"длительность {dur}")

T = lambda lang, gen: SimpleNamespace(language_code=lang, is_generated=gen)  # noqa: E731
best = yt._pick([T("en", False), T("ru", True), T("ru", False), T("de", False)])
check((best.language_code, best.is_generated) == ("ru", False), "русские ручные важнее всего")
check(yt._pick([T("en", True), T("ru", True)]).language_code == "ru", "русские автоматические важнее английских")
check(yt._pick([T("de", True), T("en", True)]).language_code == "en", "английские важнее прочих")
check(yt._pick([]) is None, "пусто — None")


async def fake_oembed(url):
    return "Лекция по кардиологии", "Кафедра"


yt._oembed = fake_oembed
chosen = SimpleNamespace(language_code="ru", is_generated=True)
fetched = SimpleNamespace(snippets=[sn("привет", 0, 2), sn("мир", 2, 2)])
yt._fetch_sync = lambda vid: (chosen, fetched)
v = asyncio.run(yt.fetch_video_text(f"https://youtu.be/{ID}"))
check(v.error is None and v.title == "Лекция по кардиологии" and v.is_generated and v.language == "ru" and v.duration_sec == 4, f"успех: {v}")
check(v.url == f"https://www.youtube.com/watch?v={ID}", "адрес приводится к виду watch?v=")

import youtube_transcript_api as y  # noqa: E402


def boom(exc):
    def f(vid):
        raise exc
    return f


for exc, code in [
    (y.TranscriptsDisabled(ID), "no_captions"),
    (y.VideoUnavailable(ID), "unavailable"),
    (y.AgeRestricted(ID), "age"),
    (y.IpBlocked(ID), "blocked"),
    (RuntimeError("что-то"), "failed"),
]:
    yt._fetch_sync = boom(exc)
    r = asyncio.run(yt.fetch_video_text(f"https://youtu.be/{ID}"))
    check(r.error_code == code and r.error and not r.text, f"{type(exc).__name__} → {code}, получилось {r.error_code}")

r = asyncio.run(yt.fetch_video_text("https://example.com/x"))
check(r.error_code == "bad_url", "чужая ссылка — bad_url")

yt._fetch_sync = lambda vid: (chosen, SimpleNamespace(snippets=[sn("x", 0, yt.MAX_DURATION_SEC + 10)]))
check(asyncio.run(yt.fetch_video_text(f"https://youtu.be/{ID}")).error_code == "too_long", "длиннее четырёх часов — too_long")
yt._fetch_sync = lambda vid: (chosen, SimpleNamespace(snippets=[sn("[Музыка]", 0, 3)]))
check(asyncio.run(yt.fetch_video_text(f"https://youtu.be/{ID}")).error_code == "no_captions", "только шум — пустые субтитры")

print(f"OK: {checks} проверок")
