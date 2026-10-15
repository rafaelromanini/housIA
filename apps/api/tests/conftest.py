import os
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("FHIR_MODE", "local")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import clock  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.fhir_client.client import LocalFhirStore, set_fhir  # noqa: E402
from app.seed.scenarios import build_demo_resources  # noqa: E402
from app.timeline.record import PatientRecord  # noqa: E402

# Âncora fixa (15:00 em São Paulo) e relógio congelado: testes não dependem da hora em que rodam.
ANCHOR = datetime.combine(date.today(), time(15, 0), tzinfo=ZoneInfo("America/Sao_Paulo")).astimezone(UTC)
clock.freeze(ANCHOR + timedelta(minutes=1))


@pytest.fixture(scope="session")
def store() -> LocalFhirStore:
    return LocalFhirStore.from_resources(build_demo_resources(anchor=ANCHOR))


@pytest.fixture
def record(store):
    def _load(pid: str) -> PatientRecord:
        rec = PatientRecord.load(store, pid)
        assert rec is not None
        rec.now = ANCHOR
        return rec

    return _load


@pytest.fixture
def client(tmp_path, store):
    db_session.configure_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(db_session.engine)
    set_fhir(store)
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    set_fhir(None)


def login(client: TestClient, role: str = "medico") -> dict:
    email = {"medico": "medico@housia.dev", "enfermagem": "enfermeira@housia.dev", "gestor": "gestor@housia.dev"}[role]
    r = client.post("/auth/login", json={"email": email, "password": "housia123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client):
    return lambda role="medico": login(client, role)
