"""Gatilhos que barram UPDATE e DELETE no audit_log dentro do próprio banco (#53). Usados pela migração c304."""

POSTGRES_UP = [
    """
    CREATE FUNCTION audit_log_imutavel() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log é somente de acréscimo (% bloqueado)', TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER audit_log_sem_alteracao BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_imutavel()
    """,
    """
    CREATE TRIGGER audit_log_sem_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_imutavel()
    """,
]
POSTGRES_DOWN = [
    "DROP TRIGGER IF EXISTS audit_log_sem_truncate ON audit_log",
    "DROP TRIGGER IF EXISTS audit_log_sem_alteracao ON audit_log",
    "DROP FUNCTION IF EXISTS audit_log_imutavel()",
]
SQLITE_UP = [
    """
    CREATE TRIGGER audit_log_sem_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log é somente de acréscimo (UPDATE bloqueado)'); END
    """,
    """
    CREATE TRIGGER audit_log_sem_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log é somente de acréscimo (DELETE bloqueado)'); END
    """,
]
SQLITE_DOWN = ["DROP TRIGGER IF EXISTS audit_log_sem_delete", "DROP TRIGGER IF EXISTS audit_log_sem_update"]


def statements(dialect: str, up: bool = True) -> list[str]:
    if dialect == "postgresql":
        return POSTGRES_UP if up else POSTGRES_DOWN
    if dialect == "sqlite":
        return SQLITE_UP if up else SQLITE_DOWN
    return []
