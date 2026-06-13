from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    TELEGRAM_BOT_TOKEN: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4.1"

    DATABASE_URL: str = ""

    ADMIN_IDS_RAW: str = ""

    EMBEDDING_MODEL_NAME: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    # Реранкер (cross-encoder). Топовый по качеству — с RAM ≥16 ГБ влезает спокойно.
    # При нехватке RAM можно вернуть лёгкий:
    # RERANKER_MODEL_NAME=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-v2-m3"
    # Размер батча реранка — больше при достаточном RAM/CPU, ниже снижает пик памяти.
    RERANK_BATCH_SIZE: int = 16

    FREE_DAILY_LIMIT: int = 10

    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 50
    # Сколько кандидатов достаём вектором перед реранком и сколько оставляем после.
    RETRIEVAL_CANDIDATES: int = 20
    RERANK_TOP_K: int = 5
    # Порог релевантности реранкера (0..1). Ниже — фрагмент считается нерелевантным.
    # TODO: откалибровать через scripts/eval_rag.py на реальных вопросах.
    RERANK_SCORE_THRESHOLD: float = 0.3
    # Запасной косинусный порог, если реранкер недоступен.
    MAX_DISTANCE_THRESHOLD: float = 0.5

    # Проверочный проход: второй вызов LLM сверяет ответ с контекстом и при выдумке
    # запускает корректирующую перегенерацию. Можно выключить ради экономии/скорости.
    VERIFY_GROUNDING: bool = True

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        # Railway-плагин Postgres отдаёт DATABASE_URL со схемой postgres(ql)://,
        # а нам нужен asyncpg-драйвер для SQLAlchemy.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @property
    def ADMIN_IDS(self) -> list[int]:
        return [int(x) for x in self.ADMIN_IDS_RAW.split(",") if x.strip()]


settings = Settings()
