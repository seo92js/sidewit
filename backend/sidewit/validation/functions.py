"""Closed SQL function policy. Names are SQLGlot canonical names, not SQL prefixes.

This limits the operations Sidewit returns; it is not a database privilege boundary.
An application's search_path, overloads, casts and operators remain its responsibility.
"""

from sqlglot import exp

from sidewit.domain.issue import Issue

# Include SQL syntax represented as Func (CAST, CASE, ARRAY, etc.). Clock functions are
# separately restricted by Generate's reference-time policy.
BUILTINS = frozenset(
    """
abs acos and array array_agg array_contains array_position array_size array_to_string asin
atan atan2 avg bit_length case cast ceil coalesce collate concat concat_ws count cume_dist
current_date current_time current_timestamp date date_add date_sub date_trunc decode
dense_rank encode ends_with exists exp exploding_generate_series extract first_value floor
generate_series greatest group_concat if initcap j_s_o_n_b_contains_top_key json_extract
json_extract_scalar jsonb_contains jsonb_extract jsonb_extract_scalar lag last_value lead
least left length ln localtime localtimestamp log logical_and logical_or lower max md5 median
min nth_value ntile nullif or percent_rank percentile_cont percentile_disc power rand rank
regexp_extract regexp_extract_all regexp_like regexp_replace replace reverse right round
row_number sign split_part sqrt starts_with stddev stddev_pop stddev_samp str_position
str_to_time substring sum time_to_str timestamp_add timestamp_sub timestamp_trunc to_char
translate trim trunc try_cast unnest upper variance
""".split()
)

# PostgreSQL spellings SQLGlot leaves as Anonymous. Do not grant canonical AST names
# (e.g. timestamp_trunc) permission to call an unrelated user-defined function.
ANONYMOUS_BUILTINS = frozenset(
    """
all age cardinality clock_timestamp every json_array json_array_elements json_array_length
json_build_array json_build_object json_each json_typeof jsonb_agg jsonb_array_elements
jsonb_array_length jsonb_build_array jsonb_build_object jsonb_each jsonb_typeof
make_date make_timestamptz octet_length some statement_timestamp timeofday
to_json to_jsonb transaction_timestamp
""".split()
)

POSTGIS = frozenset(
    """
st_area st_astext st_asewkt st_asbinary st_asgeojson st_centroid st_contains
st_coveredby st_covers st_crosses st_distance st_distancesphere st_distancespheroid
st_dwithin st_envelope st_equals st_geometrytype st_geomfromtext st_geomfromewkt
st_geomfromgeojson st_intersects st_isempty st_isvalid st_length st_makeenvelope
st_makeline st_makepoint st_point st_setsrid st_srid st_transform st_touches
st_within st_x st_y st_z
""".split()
)


def function_issues(ast: exp.Expression, extensions: list[str]) -> list[Issue]:
    spatial = POSTGIS if "postgis" in extensions else frozenset()
    anonymous_allowed = ANONYMOUS_BUILTINS | spatial
    canonical_allowed = BUILTINS | spatial
    issues = []
    for node in ast.find_all(exp.Func):
        name = node.name.lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()
        allowed = anonymous_allowed if isinstance(node, exp.Anonymous) else canonical_allowed
        # Schema-qualified calls can resolve to entirely different implementations.
        qualified = isinstance(node.parent, exp.Dot)
        table = node.find_ancestor(exp.Table)
        qualified = qualified or bool(table and (table.args.get("db") or table.args.get("catalog")))
        if qualified or name not in allowed:
            issues.append(
                Issue(
                    code="function_not_allowed",
                    message=f"Function '{name}' is not supported by the SQL function policy.",
                    ref=name,
                )
            )
    return issues
