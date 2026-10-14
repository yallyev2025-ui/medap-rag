"""Стенд замеров Medap Slide прямо из админки: примеры презентаций Luna и Sonnet готовыми файлами.

Что делает режим «examples» (решение владельца 14.10.2026): только черновик (без сокращений, зрения,
пересказа и доклада — минимум расходов), 10 слайдов, три темы, только Luna и Sonnet. Потом движок вёрстки
(`slides-engine/`, Node) собирает из черновиков PPTX и PDF, а всё готовое — файлы, таблица стоимости, лог —
сохраняется в БД: выкладка бота (сброс /tmp) ничего не стирает, повторный прогон не нужен.

Стенд — отдельный Node-скрипт `scripts/slides-bench/slides-bench.mjs`; здесь только запуск подпроцессов,
сохранение результатов и выдача файлов. Цены и формулы стоимости не дублируются.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from config import settings
from db.models import SlidesBenchFile, SlidesBenchRun
from db.session import async_session

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "slides-bench" / "slides-bench.mjs"
# В образе движок ставится в /opt/slides-engine (там node_modules); в репозитории лежит копия без node_modules.
ENGINE_DIR = Path(os.environ.get("SLIDES_ENGINE_DIR", "/opt/slides-engine"))
WORK_ROOT = Path(tempfile.gettempdir()) / "slides-bench"

MODES = {
    "examples": "Сделать примеры презентаций (Luna и Sonnet)",
    "list": "Список моделей по ключам",
    "check": "Проверка ключей и связи",
    "dry": "Оценка расхода без запросов",
}

# Короткие имена моделей в названиях файлов: med-luna.pptx, med-sonnet.pdf
SHORT = {"luna": "luna", "sonnet": "sonnet"}

CONTENT_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
}

_state: dict[str, Any] = {"run_id": None, "mode": None, "status": "idle", "started": 0.0, "finished": 0.0, "log_file": None}
_task: asyncio.Task | None = None
# Подмена фабрики сессий в тестах
_session = async_session


def node_path() -> str | None:
    return shutil.which("node")


def _engine_ready() -> bool:
    return (ENGINE_DIR / "render-drafts.mjs").exists() and (ENGINE_DIR / "node_modules").exists()


def keys_present() -> dict[str, bool]:
    env = _child_env()
    return {"OpenAI": bool(env.get("OPENAI_API_KEY")), "Anthropic": bool(env.get("ANTHROPIC_API_KEY"))}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("OPENAI_API_KEY") and settings.OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
    return env


def _stand(*args: str) -> list[str]:
    return [node_path() or "node", str(SCRIPT), *args]


def status() -> dict[str, Any]:
    if not _state["started"]:
        elapsed = 0.0
    else:
        elapsed = (time.time() if _state["status"] == "running" else _state["finished"]) - _state["started"]
    return {
        "run_id": _state["run_id"],
        "mode": _state["mode"],
        "status": _state["status"],
        "elapsed": int(elapsed),
    }


def read_live_log(limit: int = 60_000) -> str:
    """Лог идущего прогона (из временного файла); у завершённых — из БД, см. run_details."""
    path = _state.get("log_file")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except FileNotFoundError:
        return ""


async def list_runs(limit: int = 15) -> list[SlidesBenchRun]:
    async with _session() as session:
        rows = await session.execute(select(SlidesBenchRun).order_by(SlidesBenchRun.id.desc()).limit(limit))
        return list(rows.scalars())


async def run_details(run_id: int | None = None) -> tuple[SlidesBenchRun | None, list[dict[str, Any]]]:
    """Прогон (по умолчанию последний «examples») и список его файлов без содержимого."""
    async with _session() as session:
        if run_id is None:
            row = await session.execute(
                select(SlidesBenchRun).where(SlidesBenchRun.mode == "examples").order_by(SlidesBenchRun.id.desc()).limit(1)
            )
        else:
            row = await session.execute(select(SlidesBenchRun).where(SlidesBenchRun.id == run_id))
        run = row.scalars().first()
        if run is None:
            return None, []
        rows = await session.execute(
            select(
                SlidesBenchFile.id,
                SlidesBenchFile.name,
                SlidesBenchFile.size_bytes,
                SlidesBenchFile.sha256,
            )
            .where(SlidesBenchFile.run_id == run.id)
            .order_by(SlidesBenchFile.name)
        )
        files = [{"id": r.id, "name": r.name, "size_bytes": r.size_bytes, "sha256": r.sha256} for r in rows]
        return run, files


async def get_file(file_id: int) -> SlidesBenchFile | None:
    async with _session() as session:
        return await session.get(SlidesBenchFile, file_id)


async def start(mode: str, usd_rub: float) -> str | None:
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
    if mode == "examples" and not _engine_ready():
        return "В образе нет движка вёрстки презентаций (/opt/slides-engine): выложите бота заново с обновлённым Dockerfile."
    if not (usd_rub > 0):
        return "Курс доллара должен быть больше нуля."

    async with _session() as session:
        run = SlidesBenchRun(mode=mode, status="running", usd_rub=usd_rub)
        session.add(run)
        await session.commit()
        run_id = run.id

    work = WORK_ROOT / str(run_id)
    work.mkdir(parents=True, exist_ok=True)
    _state.update(run_id=run_id, mode=mode, status="running", started=time.time(), finished=0.0, log_file=str(work / "run.log"))
    _task = asyncio.create_task(_run(run_id, mode, usd_rub, work))
    return None


async def _exec(cmd: list[str], log, cwd: Path) -> int:
    log.write(f"$ {' '.join(cmd[1:])}\n".encode())
    log.flush()
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=log, stderr=asyncio.subprocess.STDOUT, env=_child_env(), cwd=str(cwd))
    return await proc.wait()


def _short_model(model: str) -> str:
    for key, short in SHORT.items():
        if key in model:
            return short
    return "".join(ch if ch.isalnum() else "-" for ch in model)[:24]


def drafts_from_results(results: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Черновики из out.json стенда → список для движка вёрстки. Второй результат — что пропущено и почему."""
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    for r in results.get("runs", []):
        name = f"{r.get('topic')}-{_short_model(str(r.get('model')))}"
        draft = r.get("draft")
        if not isinstance(draft, dict) or not draft.get("slides"):
            skipped.append(f"{name}: модель не вернула разбираемый черновик ({r.get('error') or 'нет слайдов'})")
            continue
        items.append({"name": name, "topic": r.get("topic"), "draft": draft})
    return items, skipped


def _add_file(session, run_id: int, name: str, data: bytes) -> None:
    session.add(
        SlidesBenchFile(
            run_id=run_id,
            name=name,
            content_type=CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream"),
            content=data,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
    )


async def _run(run_id: int, mode: str, usd_rub: float, work: Path) -> None:
    code = 0
    table = ""
    files: list[tuple[str, bytes]] = []
    rate = str(usd_rub)
    log_path = work / "run.log"
    try:
        with log_path.open("wb") as log:
            if mode == "list":
                code = await _exec(_stand("--list-models"), log, work)
            elif mode == "check":
                code = await _exec(_stand("--check", "--usd-rub", rate), log, work)
            elif mode == "dry":
                code = await _exec(
                    _stand("--dry-run", "--phases", "draft", "--sizes", "10", "--models", "luna,sonnet", "--usd-rub", rate), log, work
                )
            else:  # examples
                out_json = work / "out.json"
                table_file = work / "stend.md"
                code = await _exec(
                    _stand("--phases", "draft", "--sizes", "10", "--models", "luna,sonnet", "--usd-rub", rate, "--out", str(out_json)),
                    log,
                    work,
                )
                if code == 0 and out_json.exists():
                    await _exec(_stand("merge", str(out_json), "--usd-rub", rate, "--no-pack", "--out", str(table_file)), log, work)
                    if table_file.exists():
                        table = table_file.read_text(encoding="utf-8")
                    results = json.loads(out_json.read_text(encoding="utf-8"))
                    items, skipped = drafts_from_results(results)
                    for line in skipped:
                        log.write(f"⚠ {line}\n".encode())
                    log.flush()
                    files.append(("results.json", out_json.read_bytes()))
                    if items:
                        drafts_json = work / "drafts.json"
                        drafts_json.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
                        render_dir = work / "files"
                        code = await _exec(
                            [node_path() or "node", str(ENGINE_DIR / "render-drafts.mjs"), str(drafts_json), str(render_dir)], log, work
                        )
                        if render_dir.exists():
                            for f in sorted(render_dir.iterdir()):
                                files.append(("report-verstki.json" if f.name == "report.json" else f.name, f.read_bytes()))
                    else:
                        code = 1
                        log.write("✗ Ни один черновик не разобрался — собирать нечего.\n".encode())
        if mode == "examples" and code == 0 and not any(n.endswith(".pptx") for n, _ in files):
            code = 1
    except Exception as exc:  # noqa: BLE001 — любая причина должна дойти до владельца текстом
        logger.exception("Стенд замеров упал")
        with log_path.open("ab") as log:
            log.write(f"\n✗ Не удалось выполнить: {type(exc).__name__}: {exc}\n".encode())
        code = 1

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    try:
        async with _session() as session:
            run = await session.get(SlidesBenchRun, run_id)
            if run is not None:
                run.status = "done" if code == 0 else "error"
                run.log = log_text
                run.table_md = table
                run.finished_at = datetime.now(timezone.utc)
            for name, data in files:
                _add_file(session, run_id, name, data)
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить результаты стенда в БД")
        code = 1
    _state.update(status="done" if code == 0 else "error", finished=time.time())
