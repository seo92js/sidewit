"""Apply the SQL log privacy boundary for API, CLI and library entry points."""

from sidewit.security import protect_sql_logs

protect_sql_logs()
