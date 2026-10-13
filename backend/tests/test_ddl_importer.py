import pytest

from sidewit.ddl.importer import DdlParseError, import_ddl
from sidewit.domain.context import ContextDefinition, definition_issues

DDL = """
CREATE TABLE public.customers (
  id bigint PRIMARY KEY,
  region text NOT NULL,
  email varchar(255) NULL
);

CREATE TABLE orders (
  id bigserial,
  customer_id bigint NOT NULL REFERENCES customers(id),
  amount numeric(12,2),
  status text NOT NULL,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (id)
);

CREATE TABLE payments (
  id bigint,
  order_id bigint,
  CONSTRAINT payments_pk PRIMARY KEY (id),
  CONSTRAINT payments_order_fk FOREIGN KEY (order_id) REFERENCES orders
);

CREATE INDEX orders_created_at_idx ON orders (created_at);
"""


def run(ddl: str, existing: ContextDefinition | None = None, mode: str = "merge"):
    return import_ddl(existing or ContextDefinition(), ddl, "postgresql", mode)  # type: ignore[arg-type]


def field(definition: ContextDefinition, ref: str):
    resource_name, _, field_name = ref.partition(".")
    resource = definition.find_resource(resource_name)
    assert resource is not None
    return next(f for f in resource.fields if f.name == field_name)


def relations(definition: ContextDefinition) -> list[tuple[str, str]]:
    return [(r.from_, r.to) for r in definition.relations]


def test_parses_resources_fields_and_types():
    result = run(DDL)
    d = result.definition
    assert [r.name for r in d.resources] == ["customers", "orders", "payments"]
    assert [f.name for f in d.find_resource("orders").fields] == [
        "id",
        "customer_id",
        "amount",
        "status",
        "created_at",
    ]
    assert field(d, "orders.amount").type == "decimal(12, 2)"
    assert field(d, "orders.created_at").type == "timestamptz"
    assert definition_issues(d) == []


def test_primary_keys_inline_table_level_and_named_constraint():
    d = run(DDL).definition
    for ref in ("customers.id", "orders.id", "payments.id"):
        f = field(d, ref)
        assert f.primary_key and not f.nullable, ref
    assert not field(d, "orders.customer_id").primary_key


def test_nullability():
    d = run(DDL).definition
    assert field(d, "customers.region").nullable is False
    assert field(d, "customers.email").nullable is True
    assert field(d, "orders.amount").nullable is True


def test_foreign_keys_inline_and_constraint_referencing_primary_key():
    d = run(DDL).definition
    assert relations(d) == [
        ("orders.customer_id", "customers.id"),
        ("payments.order_id", "orders.id"),
    ]


def test_alter_table_foreign_key():
    ddl = """
    CREATE TABLE a (id int PRIMARY KEY);
    CREATE TABLE b (id int PRIMARY KEY, a_id int);
    ALTER TABLE ONLY public.b ADD CONSTRAINT b_a_fk FOREIGN KEY (a_id) REFERENCES public.a(id);
    """
    assert relations(run(ddl).definition) == [("b.a_id", "a.id")]


def test_warnings_for_schema_qualifier_and_ignored_statements():
    warnings = [w.message for w in run(DDL).warning_details]
    assert "Schema qualifier removed: 'public.customers' → 'customers'." in warnings
    assert any(w.startswith("Ignored statement: CREATE INDEX") for w in warnings)


def test_foreign_key_to_table_outside_ddl_is_skipped_with_warning():
    result = run("CREATE TABLE orders (id int, user_id int REFERENCES users(id));")
    assert result.definition.relations == []
    assert any(
        "orders.user_id" in w and "not in the context" in w
        for w in [w.message for w in result.warning_details]
    )


def test_composite_foreign_key_is_skipped():
    ddl = """
    CREATE TABLE a (x int, y int, PRIMARY KEY (x, y));
    CREATE TABLE b (x int, y int, FOREIGN KEY (x, y) REFERENCES a (x, y));
    """
    result = run(ddl)
    assert result.definition.relations == []
    assert any("composite foreign key" in w for w in [w.message for w in result.warning_details])


def test_duplicate_table_and_column_keep_first():
    ddl = "CREATE TABLE t (id int, id text); CREATE TABLE T (other int);"
    result = run(ddl)
    resource = result.definition.find_resource("t")
    assert [(f.name, f.type) for f in resource.fields] == [("id", "int")]
    assert len(result.definition.resources) == 1
    assert len([w.message for w in result.warning_details]) == 2


def test_parse_error():
    with pytest.raises(DdlParseError):
        run("CREATE TABLE (")


EXISTING = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "description": "주문",
                "fields": [
                    {"name": "id", "type": "int"},
                    {
                        "name": "amount",
                        "type": "int",
                        "description": "실제 결제 금액",
                        "allowed_values": [100],
                    },
                    {"name": "legacy", "type": "text"},
                ],
            },
            {"name": "audit_log", "fields": [{"name": "id", "type": "int"}]},
        ],
        "relations": [],
        "rules": [{"id": "rule_1", "text": "취소 주문은 매출에서 제외한다."}],
    }
)


def test_merge_keeps_descriptions_and_updates_types():
    d = run(DDL, EXISTING, "merge").definition
    amount = field(d, "orders.amount")
    assert amount.description == "실제 결제 금액"
    assert amount.allowed_values == [100]
    assert amount.type == "decimal(12, 2)"
    assert d.find_resource("orders").description == "주문"
    assert field(d, "orders.id").primary_key is True


def test_merge_keeps_items_missing_from_ddl_with_warnings():
    result = run(DDL, EXISTING, "merge")
    d = result.definition
    assert [r.name for r in d.resources] == ["orders", "audit_log", "customers", "payments"]
    assert field(d, "orders.legacy").type == "text"
    assert "Field 'orders.legacy' is not in the DDL; kept." in [
        w.message for w in result.warning_details
    ]
    assert "Resource 'audit_log' is not in the DDL; kept as is." in [
        w.message for w in result.warning_details
    ]
    assert d.rules == EXISTING.rules
    assert definition_issues(d) == []


def test_merge_does_not_duplicate_existing_relation():
    existing = run(DDL).definition
    d = run(DDL, existing, "merge").definition
    assert len(d.relations) == 2


def test_replace_discards_existing_schema_but_keeps_rules():
    d = run(DDL, EXISTING, "replace").definition
    assert [r.name for r in d.resources] == ["customers", "orders", "payments"]
    assert field(d, "orders.amount").description is None
    assert d.rules == EXISTING.rules


def test_qualified_table_collision_is_not_silently_merged():
    with pytest.raises(DdlParseError, match="collapse to the same Context name"):
        run("CREATE TABLE sales.items (id int); CREATE TABLE archive.items (id int);")


def test_pending_implicit_primary_key_resolves_on_later_import():
    first = run("CREATE TABLE orders (user_id int REFERENCES users)")
    assert len(first.pending_foreign_keys) == 1
    second = import_ddl(
        first.definition,
        "CREATE TABLE users (id int PRIMARY KEY)",
        "postgresql",
        "merge",
        first.pending_foreign_keys,
    )
    assert not second.pending_foreign_keys
    assert relations(second.definition) == [("orders.user_id", "users.id")]


def test_warning_codes_preserve_legacy_messages():
    existing = run("CREATE TABLE kept (id int)").definition
    result = run(
        "CREATE TABLE child (id int REFERENCES missing(id)); CREATE INDEX idx ON child(id)",
        existing,
    )
    assert {w.code for w in result.warning_details} == {
        "ignored_statement",
        "resource_preserved",
        "pending_foreign_key",
    }
