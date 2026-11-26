import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admin_id: int
    database_path: Path


def load_config() -> Config:
    """Read local settings without exposing credentials in errors."""
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите BOT_TOKEN в файле .env.")
    try:
        admin_id = int(os.getenv("ADMIN_ID", ""))
        if admin_id <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("ADMIN_ID должен быть положительным числом.") from None
    return Config(token, admin_id, Path(os.getenv("DATABASE_PATH") or "barber.sqlite3"))
