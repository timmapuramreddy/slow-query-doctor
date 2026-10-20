import os
from pathlib import Path

import pytest

from sqd import cli, db

needs_db = pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")


@pytest.mark.parametrize("command", [["check", "q.sql"], ["compare", "q.sql"]])
def test_timeout_flag_defaults_and_accepts_zero(command):
    parser = cli.build_parser()
    assert parser.parse_args(command).timeout == db.DEFAULT_TIMEOUT_S == 300
    assert parser.parse_args(command + ["--timeout", "0"]).timeout == 0
    with pytest.raises(SystemExit):
        parser.parse_args(command + ["--timeout", "-1"])


def _session(conn, timeout: str) -> None:
    """Set a session statement_timeout, as a DBA's role or database setting would."""
    conn.execute(f"SET statement_timeout = '{timeout}'")
    conn.commit()


def _shown(conn) -> str:
    value = conn.execute("SHOW statement_timeout").fetchone()[0]
    conn.rollback()
    return value


@pytest.mark.integration
@needs_db
def test_timeout_lets_a_short_query_finish_and_stops_a_long_one():
    import psycopg

    with psycopg.connect(db.get_dsn()) as conn:
        db.explain_analyze(conn, "SELECT pg_sleep(0.5)", timeout=1)
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            db.explain_analyze(conn, "SELECT pg_sleep(2)", timeout=1)
    assert caught.value.sqd_limit == db.StatementLimit(ms=1000, server=False)


@pytest.mark.integration
@needs_db
def test_session_timeout_comes_back_after_success_and_after_a_stop():
    import psycopg

    from sqd.compare import compare_with_setup

    with psycopg.connect(db.get_dsn()) as conn:
        _session(conn, "7s")
        db.explain_analyze(conn, "SELECT 1", timeout=1)
        assert _shown(conn) == "7s"
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            db.explain_analyze(conn, "SELECT pg_sleep(2)", timeout=1)
        assert caught.value.sqd_limit == db.StatementLimit(ms=1000, server=False)
        assert _shown(conn) == "7s"
        query = ("q", "SELECT 1")
        compare_with_setup(conn, query, query, "ANALYZE courses", runs=1, warmup=0, timeout=1)
        assert _shown(conn) == "7s"


@pytest.mark.integration
@needs_db
@pytest.mark.parametrize(("timeout", "expected"), [(0, None), (300, db.StatementLimit(500, True))])
def test_a_shorter_session_limit_is_kept(timeout, expected):
    import psycopg

    with psycopg.connect(db.get_dsn()) as conn:
        _session(conn, "500ms")
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            db.explain_analyze(conn, "SELECT pg_sleep(1)", timeout=timeout)
        assert _shown(conn) == "500ms"
    assert caught.value.sqd_limit == expected


@pytest.mark.integration
@needs_db
@pytest.mark.parametrize(("timeout", "shown"), [("300", " (0.5 s)"), ("0", "")])
def test_cli_names_the_server_limit_when_that_one_fired(tmp_path, capsys, timeout, shown):
    from psycopg.conninfo import make_conninfo

    dsn = make_conninfo(db.get_dsn(), options="-c statement_timeout=500")
    (tmp_path / "q.sql").write_text("SELECT pg_sleep(1)")
    args = ["--dsn", dsn, "check", str(tmp_path / "q.sql"), "--timeout", timeout]
    assert cli.main(args) == 2
    err = capsys.readouterr().err
    assert f"The query ran longer than the statement_timeout{shown} set on the server" in err


@pytest.mark.integration
@needs_db
@pytest.mark.parametrize("command", [["check"], ["compare", "fast.sql"]])
def test_cli_stops_a_long_query_and_says_which_limit(tmp_path, capsys, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    Path("q.sql").write_text("SELECT pg_sleep(2)")
    Path("fast.sql").write_text("SELECT 1")
    assert cli.main([command[0], "q.sql", *command[1:], "--timeout", "1"]) == 2
    err = capsys.readouterr().err
    assert "sqd: The query ran longer than 1 s and was stopped." in err
    assert "--timeout 0" in err


@pytest.fixture
def courses_locked():
    """Another session holds a lock that makes CREATE INDEX on courses wait."""
    import psycopg

    with psycopg.connect(db.get_dsn()) as other:
        other.execute("LOCK TABLE courses IN ROW EXCLUSIVE MODE")
        yield
        other.rollback()


SETUP = "CREATE INDEX sqd_timeout_probe ON students (last_name);\nCREATE INDEX ON courses (title);"


@pytest.mark.integration
@needs_db
def test_cli_names_the_setup_statement_that_was_stopped(tmp_path, capsys, courses_locked):
    (tmp_path / "q.sql").write_text("SELECT 1")
    (tmp_path / "fix.sql").write_text(SETUP)
    args = ["compare", str(tmp_path / "q.sql"), "--setup", str(tmp_path / "fix.sql")]
    assert cli.main(args + ["--timeout", "1"]) == 2
    err = capsys.readouterr().err
    assert 'The --setup statement "CREATE INDEX ON courses (title)" ran longer than 1 s' in err


@pytest.mark.integration
@needs_db
def test_setup_stopped_midway_leaves_nothing_and_the_connection_works(courses_locked):
    import psycopg

    from sqd.compare import compare_with_setup

    query = ("q", "SELECT 1")
    with psycopg.connect(db.get_dsn()) as conn:
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            compare_with_setup(conn, query, query, SETUP, runs=1, warmup=0, timeout=1)
        assert conn.execute("SELECT to_regclass('sqd_timeout_probe')").fetchone() == (None,)
        conn.rollback()
    assert caught.value.sqd_setup == "CREATE INDEX ON courses (title)"
