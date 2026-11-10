from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    # Trello botniki bilan BIR XIL baza (Railway'dagi o'sha Postgres).
    database_url: str
    port: int = 8080
    miniapp_base_url: str = ""
    # Kun jadvali (Toshkent, HH:MM). Standart — rahbar talabi; lokal sinovda
    # .env orqali "hozirdan 10 daqiqa" qilib qo'yish mumkin.
    checklist_start: str = "19:00"
    checklist_deadline: str = "21:00"
    checklist_remind_every_min: int = 30

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def async_database_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
