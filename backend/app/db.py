from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    # An unreachable host would otherwise hang `/health/ready` past the deploy
    # script's wait instead of answering 503.
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
