"""Smoke-test the built wheel itself, with no unpacked source-tree resource fallback."""

import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    assert wheel.is_file(), f"Wheel not found: {wheel}"
    sys.path.insert(0, str(wheel))

    import sidewit
    from sidewit.domain.context import ContextDefinition
    from sidewit.generation.planners import GenerationInput, planner_for

    assert str(sidewit.__file__).startswith(str(wheel)), sidewit.__file__
    request = GenerationInput(question="Count rows", reference_time=datetime.now(UTC))
    empty = ContextDefinition()
    metric = ContextDefinition.model_validate(
        {
            "resources": [{"name": "orders", "fields": [{"name": "id", "type": "bigint"}]}],
            "metrics": [
                {
                    "id": "order_count",
                    "description": "All orders",
                    "resource": "orders",
                    "aggregate": {"kind": "row_count"},
                }
            ],
        }
    )
    for definition, allow_sql in ((empty, True), (metric, True), (metric, False)):
        planner = planner_for(definition, allow_sql, "postgresql", "UTC", request)
        assert planner.system_prompt().strip(), planner.prompt_version
        print(f"Wheel prompt OK: {planner.prompt_version}")


if __name__ == "__main__":
    main()
