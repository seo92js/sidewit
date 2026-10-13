import uuid

DEFINITION = {
    "resources": [
        {
            "name": "customers",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "region", "type": "text"},
            ],
        },
        {
            "name": "orders",
            "description": "주문",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "customer_id", "type": "bigint"},
                {"name": "amount", "type": "numeric", "description": "실제 결제 금액"},
            ],
        },
    ],
    "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
    "rules": [{"id": "rule_1", "text": "취소 주문은 매출에서 제외한다."}],
}


async def create(client, **overrides):
    body = {"name": "commerce", "dialect": "postgresql", "definition": DEFINITION} | overrides
    res = await client.post("/api/v1/contexts", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def update_body(context, **overrides):
    body = {
        "name": context["name"],
        "dialect": context["dialect"],
        "timezone": context["timezone"],
        "allow_generated_sql": context["allow_generated_sql"],
        "definition": context["definition"],
        "expected_version": context["version"],
    }
    return body | overrides


async def test_create_and_get(client):
    created = await create(client, timezone="Asia/Seoul")
    assert created["version"] == 1
    assert created["timezone"] == "Asia/Seoul"
    assert created["definition"]["relations"][0]["from"] == "orders.customer_id"

    res = await client.get(f"/api/v1/contexts/{created['id']}")
    assert res.status_code == 200
    assert res.json() == created


async def test_create_defaults(client):
    res = await client.post("/api/v1/contexts", json={"name": "empty", "dialect": "postgresql"})
    assert res.status_code == 201
    body = res.json()
    assert body["timezone"] == "UTC"
    assert body["definition"] == {
        "resources": [],
        "relations": [],
        "rules": [],
        "metrics": [],
        "sql_extensions": [],
    }


async def test_list_is_sorted_by_name(client):
    await create(client, name="b")
    await create(client, name="a")
    res = await client.get("/api/v1/contexts")
    assert [c["name"] for c in res.json()["items"]] == ["a", "b"]


async def test_duplicate_name_conflict(client):
    await create(client)
    res = await client.post("/api/v1/contexts", json={"name": "commerce", "dialect": "postgresql"})
    assert res.status_code == 409
    assert res.json()["error"] == "context_name_conflict"


async def test_unsupported_dialect_and_timezone_rejected(client):
    for body in (
        {"name": "x", "dialect": "mysql"},
        {"name": "x", "dialect": "postgresql", "timezone": "Mars/Base"},
        {"name": "  ", "dialect": "postgresql"},
    ):
        res = await client.post("/api/v1/contexts", json=body)
        assert res.status_code == 422
        assert res.json()["error"] == "invalid_request"


async def test_relation_integrity_rejected(client):
    definition = DEFINITION | {"relations": [{"from": "orders.nope", "to": "customers.id"}]}
    res = await client.post(
        "/api/v1/contexts", json={"name": "x", "dialect": "postgresql", "definition": definition}
    )
    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "invalid_context_definition"
    assert body["details"]["errors"] == [
        {
            "code": "unknown_relation_endpoint",
            "message": "Relation endpoint 'orders.nope' does not exist in the context.",
            "ref": "orders.nope",
        }
    ]


async def test_update_bumps_version_and_returns_revalidation(client):
    context = await create(client)
    definition = DEFINITION | {"rules": [{"id": "rule_1", "text": "changed"}]}
    res = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, definition=definition)
    )
    assert res.status_code == 200
    body = res.json()
    assert body["context"]["version"] == 2
    assert body["context"]["definition"]["rules"][0]["text"] == "changed"
    assert body["revalidation"] == {"checked": 0, "valid": [], "invalid": []}


async def test_update_each_versioned_attribute_bumps_version(client):
    context = await create(client)
    for change in ({"name": "renamed"}, {"timezone": "Asia/Seoul"}):
        res = await client.put(
            f"/api/v1/contexts/{context['id']}", json=update_body(context, **change)
        )
        assert res.status_code == 200
        updated = res.json()["context"]
        assert updated["version"] == context["version"] + 1
        context = updated


async def test_update_without_changes_keeps_version(client):
    context = await create(client)
    res = await client.put(f"/api/v1/contexts/{context['id']}", json=update_body(context))
    assert res.status_code == 200
    assert res.json()["context"]["version"] == 1


async def test_update_with_stale_version_conflicts(client):
    context = await create(client)
    first = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, name="first")
    )
    assert first.status_code == 200

    stale = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, name="second")
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "version_conflict"
    assert body["details"] == {"current_version": 2, "expected_version": 1}

    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    assert current["name"] == "first"


async def test_update_rejects_invalid_definition_without_changing(client):
    context = await create(client)
    definition = DEFINITION | {"relations": [{"from": "orders.nope", "to": "customers.id"}]}
    res = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, definition=definition)
    )
    assert res.status_code == 422
    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    assert current["version"] == 1


async def test_update_to_existing_name_conflicts(client):
    await create(client, name="taken")
    context = await create(client)
    res = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, name="taken")
    )
    assert res.status_code == 409
    assert res.json()["error"] == "context_name_conflict"


async def test_delete(client):
    context = await create(client)
    res = await client.delete(f"/api/v1/contexts/{context['id']}")
    assert res.status_code == 204
    res = await client.get(f"/api/v1/contexts/{context['id']}")
    assert res.status_code == 404
    assert res.json()["error"] == "context_not_found"


async def test_missing_context_is_404_everywhere(client):
    missing = uuid.uuid4()
    for method, path, body in (
        ("GET", f"/api/v1/contexts/{missing}", None),
        ("DELETE", f"/api/v1/contexts/{missing}", None),
        ("POST", f"/api/v1/contexts/{missing}/import-ddl", {"ddl": "CREATE TABLE t (id int);"}),
    ):
        res = await client.request(method, path, json=body)
        assert res.status_code == 404, (method, path)
        assert res.json()["error"] == "context_not_found"


async def test_import_ddl_merge_preview_does_not_save(client):
    context = await create(client)
    ddl = """
    CREATE TABLE orders (id bigint PRIMARY KEY, customer_id bigint, amount numeric(12,2),
                         status text NOT NULL);
    """
    res = await client.post(
        f"/api/v1/contexts/{context['id']}/import-ddl", json={"ddl": ddl, "mode": "merge"}
    )
    assert res.status_code == 200
    body = res.json()
    orders = next(r for r in body["definition"]["resources"] if r["name"] == "orders")
    amount = next(f for f in orders["fields"] if f["name"] == "amount")
    assert amount["description"] == "실제 결제 금액"
    assert amount["type"] == "decimal(12, 2)"
    assert [f["name"] for f in orders["fields"]][-1] == "status"
    assert "warnings" not in body
    assert "Resource 'customers' is not in the DDL; kept as is." in [
        w["message"] for w in body["warning_details"]
    ]

    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    assert current == context


async def test_import_ddl_preview_can_be_saved(client):
    context = await create(client, definition={"resources": [], "relations": [], "rules": []})
    ddl = """
    CREATE TABLE customers (id bigint PRIMARY KEY);
    CREATE TABLE orders (id bigint PRIMARY KEY, customer_id bigint REFERENCES customers(id));
    """
    preview = await client.post(
        f"/api/v1/contexts/{context['id']}/import-ddl", json={"ddl": ddl, "mode": "replace"}
    )
    assert preview.status_code == 200
    definition = preview.json()["definition"]
    assert definition["relations"] == [{"from": "orders.customer_id", "to": "customers.id"}]

    res = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, definition=definition)
    )
    assert res.status_code == 200
    assert res.json()["context"]["definition"] == definition


async def test_import_ddl_parse_error(client):
    context = await create(client)
    res = await client.post(
        f"/api/v1/contexts/{context['id']}/import-ddl", json={"ddl": "CREATE TABLE ("}
    )
    assert res.status_code == 422
    assert res.json()["error"] == "ddl_parse_error"


async def test_api_key_required_when_configured(client, api_key):
    res = await client.get("/api/v1/contexts")
    assert res.status_code == 401
    assert res.json() == {
        "error": "unauthorized",
        "message": "Missing or invalid API key.",
        "details": {},
    }

    res = await client.get("/api/v1/contexts", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401

    res = await client.get("/api/v1/contexts", headers={"Authorization": f"Bearer {api_key}"})
    assert res.status_code == 200


async def test_health_is_exempt_from_api_key(client, api_key):
    res = await client.get("/api/v1/health")
    assert res.status_code == 200


async def test_unknown_route_uses_error_format(client):
    res = await client.get("/api/v1/nope")
    assert res.status_code == 404
    assert res.json()["error"] == "not_found"
