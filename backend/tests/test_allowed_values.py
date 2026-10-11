"""allowed_values: the complete value set of an enum-like field, imported from ENUM/CHECK or
declared by the developer, and enforced on generated SQL and Tool inputs."""

import copy

import pytest

from sidewit.ddl.importer import import_ddl
from sidewit.domain.context import ContextDefinition, definition_issues
from sidewit.domain.metric import field_for, field_type
from sidewit.generation.metric_plan import metric_prompt
from sidewit.generation.prompt import render_context, render_system_prompt
from sidewit.validation.sql_validator import (
    allowed_values_by_parameter,
    validate_sql_operation,
)
from tests.test_generate_api import ok
from tests.test_metric_api import ask, install

# importer -------------------------------------------------------------------------------

DDL = """
CREATE TABLE orders (
  id bigint PRIMARY KEY,
  status order_status NOT NULL,
  grade int CHECK (grade IN (1, 2, 3)),
  region text CONSTRAINT region_ck CHECK (region IN ('서울', '京都')),
  kind text,
  channel text,
  note text CHECK (char_length(note) < 100),
  CHECK (kind = 'A' OR kind = 'B'),
  CONSTRAINT channel_ck CHECK ((channel IN ('web', 'app')))
);
CREATE TYPE order_status AS ENUM ('COMPLETED', 'CANCELLED');
"""


def imported(ddl=DDL, existing=None, mode="replace"):
    return import_ddl(existing or ContextDefinition(), ddl, "postgresql", mode)


def values(definition, ref):
    return field_for(definition, ref).allowed_values


def test_enum_types_and_check_lists_become_allowed_values():
    result = imported()
    d = result.definition
    assert values(d, "orders.status") == ["COMPLETED", "CANCELLED"], "type declared later"
    assert values(d, "orders.grade") == [1, 2, 3]
    assert values(d, "orders.region") == ["서울", "京都"]
    assert values(d, "orders.kind") == ["A", "B"]
    assert values(d, "orders.channel") == ["web", "app"]
    assert values(d, "orders.note") == [], "other CHECK expressions are not value lists"
    assert values(d, "orders.id") == []
    assert not any("CREATE TYPE" in w for w in result.warnings)
    assert definition_issues(d) == []


def test_enum_typed_field_is_a_string_for_metric_inputs():
    status = field_for(imported().definition, "orders.status")
    assert status.type == "order_status" and field_type(status) == "string"


def test_merge_keeps_declared_values_unless_the_ddl_declares_them():
    existing = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "orders",
                    "fields": [
                        {"name": "region", "type": "text", "allowed_values": ["부산"]},
                        {"name": "zone", "type": "text", "allowed_values": ["north"]},
                    ],
                }
            ]
        }
    )
    ddl = "CREATE TABLE orders (region text CHECK (region IN ('서울')), zone text);"
    d = imported(ddl, existing, "merge").definition
    assert values(d, "orders.region") == ["서울"], "the DDL constraint wins"
    assert values(d, "orders.zone") == ["north"], "the developer's list is kept"


@pytest.mark.parametrize(
    ("field", "expected_ref"),
    [
        ({"name": "grade", "type": "int", "allowed_values": [1, 1]}, "t.grade"),
        ({"name": "grade", "type": "int", "allowed_values": ["one"]}, "t.grade"),
        ({"name": "day", "type": "date", "allowed_values": ["yesterday"]}, "t.day"),
    ],
)
def test_invalid_allowed_values_are_rejected(field, expected_ref):
    definition = ContextDefinition.model_validate({"resources": [{"name": "t", "fields": [field]}]})
    assert [(i.code, i.ref) for i in definition_issues(definition)] == [
        ("invalid_allowed_values", expected_ref)
    ]


# validator ------------------------------------------------------------------------------

CONTEXT = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "members",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "zone", "type": "text", "allowed_values": ["京都", "札幌"]},
                    {"name": "grade", "type": "int", "allowed_values": [1, 2, 3]},
                ],
            },
            {"name": "rentals", "fields": [{"name": "member_id", "type": "bigint"}]},
        ],
        "relations": [{"from": "rentals.member_id", "to": "members.id"}],
    }
)


def errors(sql, params=None, values=None):
    result = validate_sql_operation(sql, "postgresql", CONTEXT, params or {}, values)
    return [(e.code, e.ref) for e in result.errors]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM members WHERE zone = 'Kyoto'",
        "SELECT id FROM members WHERE zone != 'kyoto'",
        "SELECT id FROM members WHERE zone IN ('京都', 'Sapporo')",
        "SELECT id FROM members WHERE 'Kyoto' = zone",
        "SELECT id FROM members WHERE grade = 4",
        "SELECT r.member_id FROM rentals r "
        "JOIN members m ON m.id = r.member_id AND m.zone = 'Kyoto'",
        "SELECT id FROM members WHERE id IN (SELECT id FROM members WHERE zone = 'Kyoto')",
    ],
)
def test_literal_outside_allowed_values_is_rejected(sql):
    found = errors(sql)
    assert len(found) == 1 and found[0][0] == "value_not_allowed"


def test_rejection_lists_the_allowed_values_for_repair():
    result = validate_sql_operation(
        "SELECT id FROM members WHERE zone = 'Kyoto'", "postgresql", CONTEXT, {}
    )
    assert "Use one of these exactly: '京都', '札幌'." in result.errors[0].message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM members WHERE zone = '京都'",
        "SELECT id FROM members WHERE zone IN ('京都', '札幌')",
        "SELECT id FROM members WHERE grade = 2",
        "SELECT id FROM members WHERE grade = '2'",
        "SELECT id FROM members WHERE grade > 9",
        "SELECT id FROM members WHERE zone LIKE 'K%'",
    ],
)
def test_allowed_or_unchecked_comparisons_pass(sql):
    assert errors(sql) == []


def test_parameter_values_are_checked_when_given():
    sql = "SELECT id FROM members WHERE zone = :zone"
    assert errors(sql, {"zone": "string"}, {"zone": "Kyoto"}) == [
        ("value_not_allowed", "members.zone")
    ]
    assert errors(sql, {"zone": "string"}, {"zone": "京都"}) == []
    assert errors(sql, {"zone": "string"}) == [], "Tool templates have no values"


def test_check_runs_only_when_the_context_declares_allowed_values():
    plain = ContextDefinition.model_validate(
        {"resources": [{"name": "t", "fields": [{"name": "a", "type": "text"}]}]}
    )
    result = validate_sql_operation("SELECT a FROM t WHERE a = 'x'", "postgresql", plain, {})
    assert "allowed_values" not in result.checks
    result = validate_sql_operation(
        "SELECT id FROM members WHERE zone = '京都'", "postgresql", CONTEXT, {}
    )
    assert "allowed_values" in result.checks


def test_parameters_map_to_their_fields_allowed_values():
    # Equality-like comparisons only: a range such as grade >= :min is not an enum value.
    sql = (
        "SELECT id FROM members "
        "WHERE zone = :zone AND grade = :grade AND grade >= :minimum AND id = :id"
    )
    assert allowed_values_by_parameter(sql, "postgresql", CONTEXT) == {
        "zone": ["京都", "札幌"],
        "grade": [1, 2, 3],
    }


# prompts --------------------------------------------------------------------------------


def test_prompts_show_allowed_values_and_the_exact_spelling_rule():
    from datetime import UTC, datetime

    rendered = render_context(CONTEXT)
    assert "  - zone (text) Allowed values (exact): '京都', '札幌'." in rendered
    prompt = render_system_prompt(CONTEXT, "postgresql", "UTC", datetime.now(UTC))
    assert "never translate, transliterate or invent a value" in prompt


def test_metric_prompt_shows_allowed_values_of_inputs():
    from datetime import UTC, datetime

    data = copy.deepcopy(CONTEXT.model_dump(by_alias=True))
    data["metrics"] = [
        {
            "id": "members_in_zone",
            "description": "Members in a zone",
            "resource": "members",
            "aggregate": {"kind": "row_count"},
            "inputs": [{"name": "zone", "type": "string", "field": "members.zone"}],
        }
    ]
    definition = ContextDefinition.model_validate(data)
    prompt = metric_prompt(definition, "UTC", datetime.now(UTC), None, None)
    assert '"allowed_values": ["京都", "札幌"]' in prompt
    assert "never translate or invent a value" in prompt


# Generate and Tools ---------------------------------------------------------------------


@pytest.fixture
async def zone_context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "zones",
            "dialect": "postgresql",
            "definition": CONTEXT.model_dump(mode="json", by_alias=True),
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_translated_value_is_repaired_to_the_allowed_spelling(client, zone_context):
    from sidewit.generation.dto import LLMParameter

    sql = "SELECT COUNT(*) FROM members WHERE zone = :zone"
    translated = ok(sql, [LLMParameter(name="zone", type="string", value="Kyoto")])
    exact = ok(sql, [LLMParameter(name="zone", type="string", value="京都")])
    provider = install(translated, exact)
    res = await ask(client, zone_context, question="京都 회원 수")
    assert res.status_code == 200, res.text
    assert res.json()["operation"]["parameters"] == {"zone": "京都"}
    repair = provider.calls[1][1][2].content
    assert "[value_not_allowed]" in repair and "'京都', '札幌'" in repair


async def test_tool_draft_and_invoke_accept_only_allowed_values(client, zone_context):
    from sidewit.generation.dto import LLMParameter

    sql = "SELECT COUNT(*) FROM members WHERE zone = :zone"
    install(ok(sql, [LLMParameter(name="zone", type="string", value="京都")]))
    generation = (await ask(client, zone_context, question="京都 회원 수")).json()
    draft = (
        await client.post(
            "/api/v1/tools/draft", json={"generation_id": generation["generation_id"]}
        )
    ).json()
    assert draft["input_schema"]["properties"]["zone"]["enum"] == ["京都", "札幌"]
    body = {k: draft[k] for k in ("input_schema", "operation", "source_generation_id")}
    body |= {"name": "zone_members", "description": "Members", "context_id": zone_context["id"]}
    assert (await client.post("/api/v1/tools", json=body)).status_code == 201
    bad = await client.post(
        "/api/v1/tools/zone_members/invoke", json={"arguments": {"zone": "Kyoto"}}
    )
    assert bad.status_code == 422
    assert bad.json()["details"]["errors"][0]["code"] == "argument_not_in_enum"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FILTER (WHERE zone = 'Kyoto') FROM members",
        "SELECT SUM(CASE WHEN zone = 'Kyoto' THEN 1 END) FROM members",
        "SELECT id FROM members WHERE zone::text = 'Kyoto'",
        "SELECT id FROM members WHERE zone = ANY(ARRAY['京都', 'Kyoto'])",
        "SELECT id FROM members WHERE zone = SOME(ARRAY['Kyoto'])",
        "SELECT id FROM members WHERE zone = ALL(ARRAY['Kyoto'])",
        "SELECT id FROM members WHERE zone <> ALL(ARRAY['京都', 'Kyoto'])",
        "SELECT id FROM members WHERE zone COLLATE \"C\" = 'Kyoto'",
        "SELECT id FROM members WHERE zone NOT IN ('Kyoto')",
        "SELECT id FROM members ORDER BY zone = 'Kyoto'",
    ],
)
def test_allowed_values_are_checked_in_every_comparison_position(sql):
    assert [code for code, _ in errors(sql)] == ["value_not_allowed"]


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("n integer CHECK (n IN (-1, 1))", [-1, 1]),
        ("n integer CHECK (n = -1 OR n = (2))", [-1, 2]),
        # pg_dump writes value lists this way.
        ("s text CHECK (s = ANY (ARRAY['A'::text, 'B'::text]))", ["A", "B"]),
        # Every CHECK holds: the intersection.
        ("s text CHECK (s IN ('A', 'B')) CHECK (s IN ('B', 'C'))", ["B"]),
        # pg_dump's varchar form casts the column too.
        (
            "status varchar(20), CONSTRAINT t_status_check CHECK (((status)::text = ANY "
            "((ARRAY['A'::character varying, 'B'::character varying])::text[])))",
            ["A", "B"],
        ),
        # Casts are applied as PostgreSQL would.
        ("code integer CHECK (code IN ('1'::integer, '2'::integer))", [1, 2]),
        ("rate numeric CHECK (rate IN ('0.5'::numeric, 1))", [0.5, 1]),
        ("on_off boolean CHECK (on_off IN ('true'::boolean))", [True]),
        # Casts are computed as PostgreSQL does: varchar(n) cuts, numeric(p, s) rounds.
        ("s varchar(9) CHECK (s IN ('AB'::varchar(5), 'ABC'::varchar(1)))", ["AB", "A"]),
        ("r numeric CHECK (r IN (1.234::numeric(4,2), 1.235::numeric(4,2)))", [1.23, 1.24]),
        ("n integer CHECK (n IN (1.5::integer, -2.5::integer))", [2, -3]),
    ],
)
def test_check_lists_are_read_and_combined(column, expected):
    definition = imported(f"CREATE TABLE t ({column});").definition
    assert definition.resources[0].fields[0].allowed_values == expected


def test_combined_table_and_column_checks_intersect():
    ddl = "CREATE TABLE t (s text CHECK (s IN ('A', 'B', 'C')), CHECK (s IN ('C', 'A')));"
    assert imported(ddl).definition.resources[0].fields[0].allowed_values == ["A", "C"]


@pytest.mark.parametrize(
    ("column", "warning"),
    [
        ("s text CHECK (s IN (lower('A'), 'b'))", "not plain literals"),
        ("s text CHECK (s IN ('A')) CHECK (s IN ('B'))", "allow no common value"),
        # PostgreSQL would round '1.5' for an integer: no exact allowed value.
        ("code integer CHECK (code IN ('1.5'::integer, 2))", "not plain literals"),
        # numeric(4, 2) holds at most 99.99: PostgreSQL raises an overflow error.
        ("r numeric CHECK (r IN (123.4::numeric(4,2)))", "not plain literals"),
        # A list the Context would reject (text for an integer field) is not imported.
        ("code integer CHECK (code IN ('a', 'b'))", "were not imported"),
    ],
)
def test_checks_that_cannot_be_imported_are_reported(column, warning):
    result = imported(f"CREATE TABLE t ({column});")
    assert result.definition.resources[0].fields[0].allowed_values == []
    assert any(warning in w for w in result.warnings), result.warnings


@pytest.mark.parametrize(
    "column",
    [
        "n integer CHECK (n > 0)",
        "a int, b int CHECK (a = b)",
        "s text CHECK (s <> 'X')",
        # A list on a truncation of the column does not limit the column ('APPLE' passes).
        "s text CHECK (s::varchar(1) IN ('A', 'B'))",
    ],
)
def test_checks_that_are_not_value_lists_are_silent(column):
    result = imported(f"CREATE TABLE t ({column});")
    assert all(f.allowed_values == [] for f in result.definition.resources[0].fields)
    assert result.warnings == []


async def test_ddl_preview_can_always_be_saved(client):
    res = await client.post(
        "/api/v1/contexts", json={"name": "ddl", "dialect": "postgresql", "definition": {}}
    )
    context = res.json()
    ddl = (
        "CREATE TABLE t (code integer CHECK (code IN ('1'::integer, '2'::integer)), "
        "bad integer CHECK (bad IN ('a')), "
        "status varchar(9) CHECK ((status::text = ANY ((ARRAY['A'::varchar])::text[]))));"
    )
    preview = await client.post(
        f"/api/v1/contexts/{context['id']}/import-ddl", json={"ddl": ddl, "mode": "replace"}
    )
    assert preview.status_code == 200, preview.text
    fields = {
        f["name"]: f["allowed_values"]
        for f in preview.json()["definition"]["resources"][0]["fields"]
    }
    assert fields == {"code": [1, 2], "bad": [], "status": ["A"]}
    saved = await client.put(
        f"/api/v1/contexts/{context['id']}",
        json={
            **{k: context[k] for k in ["name", "dialect", "timezone", "allow_generated_sql"]},
            "expected_version": context["version"],
            "definition": preview.json()["definition"],
        },
    )
    assert saved.status_code == 200, saved.text


# Trimmed from `pg_dump --schema-only` of PostgreSQL 16 (with its psql meta-commands).
PG_DUMP = r"""
\restrict d9NMsmIsriYel9eXB5zaDTUiFxer0XsbS675BMlYQLEdTpSoOVM6momfnzcwl3x
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE TYPE public.mood AS ENUM (
    'ok',
    'bad',
    'meh'
);
CREATE TABLE public.t (
    status character varying(20),
    code integer,
    m public.mood,
    n integer,
    CONSTRAINT t_code_check CHECK ((code = ANY (ARRAY['-1'::integer, 1, 2]))),
    CONSTRAINT t_code_check1 CHECK ((code = ANY (ARRAY[1, 2, 3]))),
    CONSTRAINT t_m_check CHECK ((m = ANY (ARRAY['ok'::public.mood, 'bad'::public.mood]))),
    CONSTRAINT t_n_check CHECK ((n > 0)),
    CONSTRAINT t_status_check CHECK (((status)::text = ANY ((ARRAY['A'::character varying,
        'B'::character varying])::text[])))
);
ALTER TABLE public.t OWNER TO postgres;
\unrestrict d9NMsmIsriYel9eXB5zaDTUiFxer0XsbS675BMlYQLEdTpSoOVM6momfnzcwl3x
"""


def test_real_pg_dump_output_is_imported():
    result = imported(PG_DUMP)
    fields = {f.name: f.allowed_values for f in result.definition.resources[0].fields}
    assert fields == {"status": ["A", "B"], "code": [1, 2], "m": ["ok", "bad"], "n": []}
    assert not any("t.m" in warning for warning in result.warnings)
