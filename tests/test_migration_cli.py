import pytest

from app.db import migrate


def test_migration_requires_own_credentials_and_sanitizes_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "synthetic-runtime-secret")
    assert migrate.main() == 1
    output = capsys.readouterr()
    assert "Migration failed" in output.err
    assert "synthetic-runtime-secret" not in output.err
    assert output.out == ""


def test_production_migration_rejects_neon_pooler_before_connecting(monkeypatch, capsys) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", (
        "postgresql+psycopg://owner:synthetic-secret@ep-test-pooler.eu-central-1.aws.neon.tech/gym"
        "?sslmode=verify-full"
    ))
    attempted = []

    def forbidden_engine(url):
        attempted.append(True)
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(migrate, "create_db_engine", forbidden_engine)
    assert migrate.main() == 1
    assert attempted == []
    assert "synthetic-secret" not in capsys.readouterr().err
