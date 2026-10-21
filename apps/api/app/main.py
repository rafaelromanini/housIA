import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.alerts.router import router as alerts_router
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
from app.auth.router import seed_demo_users
from app.config import get_settings
from app.consult import privacidade
from app.consult import service as consult_service
from app.consult.router import router as consult_router
from app.db import crypto, rekey
from app.db import session as db_session
from app.db.session import Base
from app.discharge.router import router as discharge_router
from app.feedback.router import router as feedback_router
from app.fhir_client.client import FhirUnavailable, get_fhir
from app.gestao.router import router as gestao_router
from app.handoff.router import router as handoff_router
from app.journey.router import router as journey_router
from app.medrec.router import router as medrec_router
from app.patients.router import router as patients_router
from app.prontidao import exigir_producao_segura

logger = logging.getLogger("housia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    exigir_producao_segura(settings)
    # Em produção o schema vem do Alembic (`alembic upgrade head` no entrypoint);
    # create_all só cobre o modo local/SQLite e é idempotente.
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(db_session.engine)
    if settings.seed_users:
        with db_session.SessionLocal() as db:
            seed_demo_users(db)
    crypto.keys()  # chave malformada derruba a subida, em vez de gravar com a chave errada
    with db_session.SessionLocal() as db:  # cifra o que ficou em texto aberto e faz o expurgo por retenção (#31)
        for job in (rekey.run, privacidade.run, consult_service.retry_outbox):
            try:
                job(db)
            except Exception:  # tabela ainda sem migrar etc.: não derruba a API
                db.rollback()
                logger.exception("Falha em %s.%s", job.__module__, job.__name__)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    exigir_producao_segura(settings)
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

    @app.exception_handler(FhirUnavailable)
    async def fhir_unavailable(_: Request, exc: FhirUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health", tags=["infra"])
    def health() -> dict:
        db_ok = True
        try:
            with db_session.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_ok = False
        fhir_ok = True
        try:
            get_fhir().locations()
        except FhirUnavailable:
            fhir_ok = False
        return {
            "status": "ok" if db_ok else "degradado",
            "db": db_ok,
            "fhir": fhir_ok,
            "fhir_mode": settings.fhir_mode,
            "llm_mode": settings.llm_mode,
            "env": settings.env,
        }

    app.include_router(auth_router)
    app.include_router(patients_router)
    app.include_router(handoff_router)
    app.include_router(gestao_router)
    app.include_router(alerts_router)
    app.include_router(medrec_router)
    app.include_router(discharge_router)
    app.include_router(audit_router)
    app.include_router(feedback_router)
    app.include_router(consult_router)
    app.include_router(journey_router)
    return app


app = create_app()
