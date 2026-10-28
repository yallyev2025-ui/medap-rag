"""Версия бота презентаций и сверка с сайтом: по команде /version владелец видит, выложено ли новое и бот, и сайт (а не одно из двух)."""

# Поднимать вместе с `BOT_API_VERSION` в apps/web/src/lib/slides/bot-api.ts, когда меняется «дверь» между ботом и сайтом.
BOT_VERSION = "2026-10-28.2"
# Что бот ждёт от сайта (те же имена, что `BOT_API_FEATURES` на сайте)
NEEDS = ("options", "articles", "photo_upload", "open_link", "suggest_photos", "create_modes", "uploads", "upload_file", "photo_auto", "review", "digest", "gift")


def compare(site_reply: dict) -> str:
    """Человеческий отчёт о версиях бота и сайта."""
    head = f"Бот презентаций: версия {BOT_VERSION}."
    if not site_reply.get("ok"):
        err = str(site_reply.get("error") or "")
        if "Неизвестное действие" in err:
            return head + "\nСайт: СТАРАЯ версия (не знает проверки версии). Выложите сайт из ветки claude/stage-4-cd2vk8 вместе с ботом — иначе выбор источников, оформления и фото не заработает."
        if site_reply.get("network"):
            return head + "\nСайт сейчас не отвечает. Проверьте, что он запущен и адрес SLIDES_SITE_URL верный."
        return head + f"\nСайт ответил ошибкой: {err or 'нет связи / не настроен SLIDES_SITE_URL, SLIDES_SITE_SECRET'}."
    have = set(site_reply.get("features") or [])
    missing = [n for n in NEEDS if n not in have]
    ver = str(site_reply.get("version") or "?")
    if missing:
        return head + f"\nСайт: версия {ver}, но не хватает возможностей: {', '.join(missing)}. Выложите свежий сайт вместе с ботом."
    if ver != BOT_VERSION:
        return head + f"\nСайт: версия {ver}. Возможности все есть, но версии разные — на всякий случай выложите обе части из свежих веток."
    return head + f"\nСайт: версия {ver}. ✅ Всё совпадает, выложено вместе."
