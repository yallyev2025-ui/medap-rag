"""Запуск стенда замеров Medap Slide прямо из админки (без веб-консоли Timeweb).

Стенд — отдельный Node-скрипт `scripts/slides-bench/slides-bench.mjs`: он делает настоящие
запросы к моделям Claude и OpenAI и считает стоимость по токенам. Здесь только запуск
подпроцесса, журнал вывода и выдача результатов; сам расчёт цен не дублируется.

Один запуск за раз. Состояние живёт в памяти процесса: после перезапуска бота (выкладки)
идущий запуск обрывается, а прошлые результаты в /tmp пропадают — файл-пакет и таблицу
нужно забрать сразу после прогона.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "slides-bench" / "slides-bench.mjs"
WORK_DIR = Path("/tmp/slides-bench")
LOG_FILE = WORK_DIR / "run.log"
OUT_JSON = WORK_DIR / "out.json"
TABLE_FILE = WORK_DIR / "stend.md"
PACK_FILE = WORK_DIR / "stend-pack.txt"

# Режимы: что показывает кнопка и какие команды стоят за ней.
MODES = {
    "list": "Список моделей по ключам",
    "check": "Проверка ключей и связи",
    "dry": "Оценка расхода без запросов",
    "run": "Боевой прогон + таблица",
}

_state: dict[str, Any] = {"mode": None, "status": "idle", "started": 0.0, "finished": 0.0, "code": None}
_task: asyncio.Task | None = None


def node_path() -> str | None:
    return shutil.which("node")


def keys_present() -> dict[str, bool]:
    env = _child_env()
    return {"OpenAI": bool(env.get("OPENAI_API_KEY")), "Anthropic": bool(env.get("ANTHROPIC_API_KEY"))}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("OPENAI_API_KEY") and settings.OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
    return env


def _commands(mode: str, usd_rub: float) -> list[list[str]]:
    node = node_path() or "node"
    base = [node, str(SCRIPT)]
    rate = ["--usd-rub", str(usd_rub)]
    if mode == "list":
        return [base + ["--list-models"]]
    if mode == "check":
        return [base + ["--check"] + rate]
    if mode == "dry":
        return [base + ["--dry-run"] + rate]
    if mode == "run":
        return [
            base + rate + ["--out", str(OUT_JSON)],
            base + ["merge", str(OUT_JSON)] + rate + ["--out", str(TABLE_FILE)],
        ]
    raise ValueError(mode)


def status() -> dict[str, Any]:
    if not _state["started"]:
        elapsed = 0.0
    else:
        elapsed = (time.time() if _state["status"] == "running" else _state["finished"]) - _state["started"]
    return {**_state, "elapsed": int(elapsed)}


def read_log(limit: int = 60_000) -> str:
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return text[-limit:]


def read_results() -> dict[str, str]:
    out = {"table": "", "pack": ""}
    try:
        out["table"] = TABLE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    try:
        out["pack"] = PACK_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    return out


def start(mode: str, usd_rub: float) -> str | None:
    """Запускает режим в фоне. Возвращает текст ошибки по-русски или None, если запуск принят."""
    global _task
    if mode not in MODES:
        return "Неизвестный режим."
    if _state["status"] == "running":
        return "Уже идёт запуск — дождитесь его конца."
    if not SCRIPT.exists():
        return f"Нет файла стенда ({SCRIPT.name}): бот выложен без него."
    if node_path() is None:
        return "В образе бота нет Node.js: выложите бота заново с обновлённым Dockerfile."
    if not (usd_rub > 0):
        return "Курс доллара должен быть больше нуля."

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "run":
        for stale in (OUT_JSON, TABLE_FILE, PACK_FILE):
            stale.unlink(missing_ok=True)
    _state.update(mode=mode, status="running", started=time.time(), finished=0.0, code=None)
    _task = asyncio.create_task(_run(mode, usd_rub))
    return None


async def _run(mode: str, usd_rub: float) -> None:
    code = 0
    try:
        with LOG_FILE.open("wb") as log:
            for cmd in _commands(mode, usd_rub):
                log.write(f"$ {' '.join(cmd[1:])}\n".encode())
                log.flush()
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT, env=_child_env(), cwd=str(WORK_DIR)
                )
                code = await proc.wait()
                if code != 0:
                    break
    except Exception as exc:  # noqa: BLE001 — любая причина должна дойти до владельца текстом
        logger.exception("Стенд замеров упал")
        LOG_FILE.open("ab").write(f"\n✗ Не удалось запустить: {type(exc).__name__}: {exc}\n".encode())
        code = 1
    _state.update(status="done" if code == 0 else "error", finished=time.time(), code=code)
