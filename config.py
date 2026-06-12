from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    TELEGRAM_BOT_TOKEN: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    DATABASE_URL: str = ""

    ADMIN_IDS_RAW: str = ""

    EMBEDDING_MODEL_NAME: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    FREE_DAILY_LIMIT: int = 10

    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 50
    RETRIEVAL_TOP_K: int = 5

    @property
    def ADMIN_IDS(self) -> list[int]:
        return [int(x) for x in self.ADMIN_IDS_RAW.split(",") if x.strip()]


settings = Settings()
