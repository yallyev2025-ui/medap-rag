# SearXNG для MedAP Student AI.
#
# Это отдельное маленькое приложение на Timeweb App Platform. Бот ходит к
# нему по адресу из переменной SEARXNG_URL. Так поиск не зависит от того,
# что Timeweb собирает основной бот только из корневого Dockerfile.
#
# Официальный образ уже содержит SearXNG и его зависимости; мы добавляем
# только настройки: JSON-формат для бота, русский язык, без лимитера
# (контейнер не публикуется в интернет как открытый поисковик).
FROM searxng/searxng:latest

COPY settings.yml /etc/searxng/settings.yml

EXPOSE 8080
