# SQL function policy

Sidewit checks every function expression against a closed allowlist, including functions
inside subqueries, CTEs and FROM sources. The check applies to /validate, generated SQL,
Tool saves and Tool invocations. It is independent of the model and cannot be changed by
instructions in a question.

Common aggregates, string, numeric, date/time, window and JSON functions are supported.
The authoritative canonical-expression lists are
[BUILTINS and POSTGIS](../backend/sidewit/validation/functions.py). SQLGlot maps some SQL
spellings to different canonical names (for example date_trunc to timestamp_trunc).

Unknown functions and schema-qualified calls are rejected with function_not_allowed.
This includes pg_sleep, pg_read_file, lo_import, set_config, sequence mutation,
administrative functions, query_to_xml and dblink. Supported table functions such as
unnest, generate_series, JSON array-element and entry functions remain available.
There is no prefix-based permission or arbitrary custom-function override.

## PostGIS

In the Context editor, enable **Allow supported PostGIS functions**, then save. API clients
can include **"sql_extensions": ["postgis"]** in the Context definition.

Merge this field into the rest of your existing definition when updating a Context; it is
not a patch request. The default is []. Both DDL import modes preserve the setting.

This enables a limited set of spatial functions, including ST_DWithin, ST_Distance,
ST_Contains, ST_Intersects, ST_MakePoint, ST_SetSRID and ST_Transform.
Unknown ST_* names are still rejected. PostGIS must already exist in the executing
database; Sidewit does not install extensions or verify function signatures.

## Strings and time

Dollar-quoted strings participate in the same placeholder and value checks as ordinary
strings. PostgreSQL escape and Unicode strings (E'...', U&'...') are rejected with
unsupported_string_literal: decoding them incompletely would permit bypasses. Bind the
value as a parameter instead.

Generate continues to reject database-clock expressions. This includes special date words
in casts, dollar strings and comparisons against known temporal fields, with aliases and
CTE lineage. Supplied string parameters containing special date words are checked when
used as dates, including through nested casts, COALESCE, CASE and scalar subqueries.
Computed text whose date interpretation cannot be verified, such as
`('to' || 'day')::date`, is rejected with relative_time_unverifiable. Supply an absolute
date/date-time parameter instead. Ordinary text such as name = 'today' remains valid.
Saved Tools retain the existing default policy that permits clock functions.

## Upgrade and execution boundary

Previously saved Tools may no longer pass. Revalidate them after upgrading; invocation
always checks the current policy. Replace unsupported calls or rewrite escape strings as
parameters. A Context-version mismatch is displayed as stale, without an old success
verdict.

An allowlisted name does not prove execution safety: database overloads, custom casts,
operators, search paths, privileges, resource cost and business meaning are outside static
validation. Use a least-privilege execution role, trusted schemas/search paths and execution
limits in the application that runs the returned operation.
