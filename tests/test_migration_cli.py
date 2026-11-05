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
