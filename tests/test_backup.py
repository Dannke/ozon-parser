"""Резервная копия базы: pg_dump в контейнере, проверка архива, ротация копий.

Docker не нужен: subprocess.run подменяется сценарием, который пишет в
stdout то же, что настоящий pg_dump, и отдаёт оглавление, как pg_restore --list.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess

import pytest

from price_panel import backup

DAY = dt.date(2026, 10, 6)
LISTING = (
    b";\n; Archive created at 2026-10-06 12:00:00 UTC\n"
    b"3412; 0 16420 TABLE DATA public price_history ozon\n"
)


def fake_docker(
    dump: bytes = b"PGDMP\x01\x0e\x00 data",
    dump_code: int = 0,
    listing: bytes = LISTING,
    calls: list | None = None,
):
    """subprocess.run для docker compose exec: pg_dump и pg_restore --list."""

    def run(command, **kwargs):
        if calls is not None:
            calls.append(command)
        if "pg_dump" in command:
            kwargs["stdout"].write(dump)
            stderr = b"pg_dump: error: connection failed" if dump_code else b""
            return subprocess.CompletedProcess(command, dump_code, stderr=stderr)
        # pg_restore получает дескриптор ОС: он должен стоять в начале файла
        # (tell() буферизованного объекта этого не гарантирует).
        stdin = kwargs["stdin"]
        assert os.lseek(stdin.fileno(), 0, os.SEEK_CUR) == 0
        assert os.read(stdin.fileno(), 5) == b"PGDMP"
        return subprocess.CompletedProcess(command, 0, stdout=listing, stderr=b"")

    return run


def test_backup_is_written_checked_and_named_by_date(tmp_path):
    calls: list = []
    path = backup.create_backup(tmp_path, keep=14, today=DAY, runner=fake_docker(calls=calls))

    assert path.name == "ozon-2026-10-06.dump"
    assert path.read_bytes().startswith(b"PGDMP")
    assert [command[:6] for command in calls] == [
        ["docker", "compose", "exec", "-T", "postgres", "pg_dump"],
        ["docker", "compose", "exec", "-T", "postgres", "pg_restore"],
    ]
    assert "-Fc" in calls[0]
    assert list(tmp_path.iterdir()) == [path]  # временного файла не осталось


def test_old_backups_are_rotated(tmp_path):
    for day in range(1, 6):
        (tmp_path / "ozon-2026-10-0{}.dump".format(day)).write_bytes(b"PGDMP")
    (tmp_path / "notes.txt").write_text("не копия", encoding="utf-8")

    backup.create_backup(tmp_path, keep=3, today=DAY, runner=fake_docker())
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "notes.txt",
        "ozon-2026-10-04.dump",
        "ozon-2026-10-05.dump",
        "ozon-2026-10-06.dump",
    ]


@pytest.mark.parametrize(
    "runner, message",
    [
        (fake_docker(dump_code=1), "connection failed"),
        (fake_docker(dump=b"<html>not a dump</html>"), "не архив"),
        (fake_docker(listing=b"; empty archive\n"), "истории цен"),
    ],
)
def test_bad_backup_keeps_previous_copies(tmp_path, runner, message):
    """Неудачная копия не занимает место прежних и не остаётся на диске."""
    previous = tmp_path / "ozon-2026-10-05.dump"
    previous.write_bytes(b"PGDMP old")
    with pytest.raises(backup.BackupError, match=message):
        backup.create_backup(tmp_path, keep=1, today=DAY, runner=runner)
    assert list(tmp_path.iterdir()) == [previous]


def test_missing_docker_is_a_clear_error(tmp_path):
    def no_docker(command, **kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(backup.BackupError, match="docker"):
        backup.create_backup(tmp_path, keep=3, today=DAY, runner=no_docker)
