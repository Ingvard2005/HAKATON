from pathlib import Path
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("CALLMIND_DATA_DIR", BASE_DIR / "data"))
AUDIO_DIR = DATA_DIR / "audio"

DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "callmind.db"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    connect_args={
        "check_same_thread": False
    }
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False
)


def init_db():
    import models

    Base.metadata.create_all(
        bind=engine
    )
