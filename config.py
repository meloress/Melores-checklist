from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    # Trello botniki bilan BIR XIL baza (Railway'dagi o'sha Postgres).
    database_url: str
    port: int = 8080
    miniapp_base_url: str = ""
    # Kun jadvali (19:00/21:00/30 daq) va ball variantlari — bazada, Mini App Sozlamalarida.

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def async_database_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
