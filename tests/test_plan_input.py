import json
import os
from pathlib import Path

import psycopg
import pytest

from sqd import cli, db
from sqd.catalog import Catalog
from sqd.plan import parse_explain, relations
from sqd.rules import run_rules

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"
SLOW_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*_slow.json"))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def no_db(monkeypatch):
    """Fail the test if anything tries to connect to a database."""

    def refuse(*args, **kwargs):
        raise AssertionError("tried to connect to a database")

    monkeypatch.setattr(psycopg, "connect", refuse)


def _write_inputs(tmp_path: Path, plan: object, catalog: object | None = None) -> list[str]:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))
    args = ["check", "--plan", str(plan_file)]
    if catalog is not None:
        catalog_file = tmp_path / "catalog.json"
        catalog_file.write_text(json.dumps(catalog))
        args += ["--catalog", str(catalog_file)]
    return args


def test_parse_explain_accepts_psql_array_and_bare_object():
    plan = _fixture("missing_index_slow")["plan"]
    assert parse_explain(json.dumps([plan])) == plan
    assert parse_explain(json.dumps(plan)) == plan


def test_parse_explain_reads_utf16_files_from_windows_shells():
    plan = _fixture("missing_index_slow")["plan"]
    assert parse_explain(json.dumps([plan]).encode("utf-16")) == plan


def test_parse_explain_accepts_analyze_with_timing_off():
    plan = {"Plan": {"Node Type": "Seq Scan", "Actual Rows": 5, "Actual Loops": 1}}
    assert parse_explain(json.dumps([plan])) == plan


@pytest.mark.parametrize(
    "text, message",
    [
        ("Seq Scan on attendance  (cost=0.00..18334.00 rows=3 width=12)", "not JSON"),
        ('[{"Plan": {"Actual Rows": 1}}, {"Plan": {"Actual Rows": 1}}]', "one plan"),
        ('{"rows": 3}', 'no "Plan"'),
        ('[{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 3}}]', "without ANALYZE"),
    ],
)
def test_parse_explain_rejects_what_the_rules_cannot_read(text, message):
    with pytest.raises(ValueError, match=message) as err:
        parse_explain(text)
    assert "FORMAT JSON" in str(err.value)


@pytest.mark.parametrize("name", SLOW_FIXTURES)
def test_check_plan_gives_the_same_report_as_a_live_check(name, tmp_path, capsys, no_db):
    data = _fixture(name)
    expected = cli.format_findings(
        run_rules(data["plan"], Catalog.from_dict(data["catalog"])),
        data["plan"]["Execution Time"],
    )
    assert cli.main(_write_inputs(tmp_path, [data["plan"]], data["catalog"])) == 0
    out, err = capsys.readouterr()
    assert out.strip() == expected
    assert err == ""


def test_check_plan_without_catalog_names_the_missing_tables(tmp_path, capsys, no_db):
    plan = _fixture("deep_offset_slow")["plan"]
    assert cli.main(_write_inputs(tmp_path, [plan])) == 0
    out, err = capsys.readouterr()
    assert "deep-offset" in out  # the one rule that needs no table info
    assert "attendance" in err
    assert "sqd catalog-sql" in err


def test_check_plan_names_tables_missing_from_a_partial_catalog(tmp_path, capsys, no_db):
    data = _fixture("join_key_slow")
    catalog = {"courses": data["catalog"]["courses"]}
    assert cli.main(_write_inputs(tmp_path, [data["plan"]], catalog)) == 0
    err = capsys.readouterr().err
    assert "enrollments" in err
    assert "courses" not in err


@pytest.mark.parametrize(
    "catalog_text, message",
    [
        ("coalesce\n----------\n{}\n(1 row)", "not JSON"),
        ('{"students": {"rows": 5}}', "sqd catalog-sql"),
        ("[1, 2]", "sqd catalog-sql"),
        ('{"students": {"rows": "5", "columns": [], "indexes": []}}', "sqd catalog-sql"),
        ('{"students": {"rows": 5, "columns": [1], "indexes": []}}', "sqd catalog-sql"),
        (
            '{"t": {"rows": 5, "columns": [], "indexes": [{"name": "i", "columns": [2], '
            '"definition": ""}]}}',
            "sqd catalog-sql",
        ),
    ],
)
def test_check_plan_rejects_a_bad_catalog_file(catalog_text, message, tmp_path, capsys, no_db):
    args = _write_inputs(tmp_path, [_fixture("missing_index_slow")["plan"]])
    (tmp_path / "catalog.json").write_text(catalog_text)
    assert cli.main(args + ["--catalog", str(tmp_path / "catalog.json")]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "args, message",
    [
        (["check"], "Give a SQL file or --plan"),
        (["check", "q.sql", "--plan", "p.json"], "not both"),
        (["check", "q.sql", "--catalog", "c.json"], "--catalog only works with --plan"),
    ],
)
def test_check_needs_exactly_one_input(args, message, capsys, no_db):
    assert cli.main(args) == 2
    assert message in capsys.readouterr().err


def test_catalog_sql_dollar_quotes_names_with_a_tag_not_in_the_name(tmp_path, capsys, no_db):
    plan = {"Plan": {"Relation Name": "o'brien", "Actual Rows": 1, "Plans": []}}
    plan["Plan"]["Plans"].append({"Relation Name": "a$n$b", "Actual Rows": 1})
    (tmp_path / "plan.json").write_text(json.dumps([plan]))
    assert cli.main(["catalog-sql", str(tmp_path / "plan.json")]) == 0
    out = capsys.readouterr().out
    assert "ARRAY[$n0$a$n$b$n0$, $n0$o'brien$n0$]::text[]" in out
    assert "%s" not in out


def test_catalog_sql_refuses_a_nul_in_a_name(tmp_path, capsys, no_db):
    plan = {"Plan": {"Relation Name": "a\u0000b", "Actual Rows": 1}}
    (tmp_path / "plan.json").write_text(json.dumps([plan]))
    assert cli.main(["catalog-sql", str(tmp_path / "plan.json")]) == 2
    assert "NUL" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_printed_catalog_sql_keeps_hostile_names_inside_the_literal():
    # With standard_conforming_strings off, a backslash escapes a quote in '...'.
    payload = "x\\'\n; CREATE TEMP TABLE injected (); --"
    with psycopg.connect(db.get_dsn()) as conn:
        conn.execute("SET LOCAL standard_conforming_strings = off")
        conn.execute('CREATE TEMP TABLE "back\\slash" (id int)')
        row = conn.execute(db.catalog_sql({payload, "back\\slash"})).fetchone()
        injected = conn.execute("SELECT to_regclass('pg_temp.injected')").fetchone()[0]
        conn.rollback()
    assert list(row[0]) == ["back\\slash"]
    assert injected is None


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_shared_plan_and_printed_catalog_match_a_live_check(tmp_path, capsys):
    with psycopg.connect(db.get_dsn()) as conn:
        for name in sorted(p.name for p in EXAMPLES.iterdir()):
            plan = db.explain_analyze(conn, (EXAMPLES / name / "slow.sql").read_text())
            live = cli.format_findings(
                run_rules(plan, db.load_catalog(conn, relations(plan))), plan["Execution Time"]
            )
            plan_file = tmp_path / "plan.json"
            plan_file.write_text(json.dumps([plan]))
            assert cli.main(["catalog-sql", str(plan_file)]) == 0
            row = conn.execute(capsys.readouterr().out).fetchone()
            conn.rollback()
            (tmp_path / "catalog.json").write_text(json.dumps(row[0]))
            args = ["check", "--plan", str(plan_file), "--catalog", str(tmp_path / "catalog.json")]
            assert cli.main(args) == 0
            assert capsys.readouterr().out.strip() == live, name


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_load_catalog_finds_mixed_case_tables():
    with psycopg.connect(db.get_dsn()) as conn:
        conn.execute('CREATE TEMP TABLE "Mixed Case" (id int, "Email" text)')
        conn.execute('CREATE INDEX ON "Mixed Case" (lower("Email"))')
        catalog = db.load_catalog(conn, {"Mixed Case", "no_such_table"})
        # psql -At prints SQL NULL as an empty line, so "none found" must be {}.
        assert db.load_catalog(conn, {"no_such_table"}).tables == {}
    assert list(catalog.tables) == ["Mixed Case"]
    table = catalog.tables["Mixed Case"]
    assert table.columns == ["id", "Email"]
    assert [ix.columns for ix in table.indexes] == [[None]]
