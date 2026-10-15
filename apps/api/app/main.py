import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.auth.router import router as auth_router
from app.auth.router import seed_demo_users
from app.config import get_settings
from app.db import session as db_session
from app.db.session import Base

logger = logging.getLogger("housia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Em produção o schema vem do Alembic (`alembic upgrade head` no entrypoint);
    # create_all só cobre o modo local/SQLite e é idempotente.
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(db_session.engine)
    if settings.seed_users:
        with db_session.SessionLocal() as db:
            seed_demo_users(db)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="HousIA API",
        version="0.1.0",
        description="Suporte à decisão em transições de cuidado. Ambiente de demonstração — dados sintéticos.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["infra"])
    def health() -> dict:
        db_ok = True
        try:
            with db_session.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_ok = False
        return {
            "status": "ok" if db_ok else "degradado",
            "db": db_ok,
            "fhir_mode": settings.fhir_mode,
            "llm_mode": settings.llm_mode,
        }

    app.include_router(auth_router)
    return app


app = create_app()
