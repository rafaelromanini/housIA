"""Job de expurgo: python -m app.consult.purge (também roda no startup da API)."""

from app.consult import privacidade
from app.db import session as db_session


def main() -> int:
    with db_session.SessionLocal() as db:
        n = privacidade.run(db)
    print(f"consultas expurgadas: {n}")
    return n


if __name__ == "__main__":
    main()
