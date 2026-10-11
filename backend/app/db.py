from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_s,
    connect_args={
        # An unreachable host would otherwise hang `/health/ready` past the deploy
        # script's wait instead of answering 503.
        "connect_timeout": 5,
        "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
    },
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
