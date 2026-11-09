import pytest

from app import release


@pytest.mark.parametrize(("execute", "migration_status"), [(False, 0), (True, 0), (True, 1)])
def test_release_plan_and_execution_are_separate(monkeypatch, capsys, execute, migration_status) -> None:
    profiles, migrations = [], []
    monkeypatch.setattr(release, "validate_profile", profiles.append)

    def migrate_once():
        migrations.append(True)
        return migration_status

    monkeypatch.setattr(release.migrate, "main", migrate_once)
    assert release.main(["--execute-migrations"] if execute else []) == (migration_status if execute else 0)
    assert profiles == ["migration"]
    assert migrations == ([True] if execute else [])
    output = capsys.readouterr().out
    assert ("STOP release_migrations" in output) == (execute and migration_status != 0)


@pytest.mark.parametrize("execute", [False, True])
def test_invalid_release_profile_never_connects_or_exposes_secrets(monkeypatch, capsys, execute) -> None:
    def invalid_profile(profile):
        raise ValueError("synthetic-sensitive-migration-url")

    def forbidden_migration():
        raise AssertionError("Invalid configuration reached the database")

    monkeypatch.setattr(release, "validate_profile", invalid_profile)
    monkeypatch.setattr(release.migrate, "main", forbidden_migration)
    assert release.main(["--execute-migrations"] if execute else []) == 1
    output = capsys.readouterr().out
    assert "FAIL release_configuration" in output
    assert "synthetic-sensitive" not in output
