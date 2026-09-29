"""Truncate ClickHouse's own internal instrumentation tables.

Unlike Langfuse's trace data (observations/traces), these system tables have
no default retention and log every single query at high volume -- they grew
past a gig in under a week and outpaced attempts to TTL them off (ClickHouse
25.12 doesn't seem to honor <table remove="remove"/> in config.d the way its
docs imply -- see 2026-09-29 session notes). A daily truncate is the ceiling:
these tables carry no data we ever query, just ClickHouse's own diagnostics.
Runs daily on the VM (langfuse-ch-log-trim.timer).
"""
import subprocess
from pathlib import Path

TABLES = ["text_log", "trace_log", "opentelemetry_span_log",
          "processors_profile_log", "query_thread_log"]
ENV = Path.home() / "langfuse" / ".env"


def clickhouse_password():
    for line in ENV.read_text().splitlines():
        if line.startswith("CLICKHOUSE_PASSWORD="):
            return line.split("=", 1)[1]
    raise SystemExit("CLICKHOUSE_PASSWORD not found in ~/langfuse/.env")


def main():
    password = clickhouse_password()
    for table in TABLES:
        subprocess.run(
            ["docker", "exec", "langfuse-clickhouse-1", "clickhouse-client",
             "--user", "clickhouse", "--password", password,
             "-q", f"TRUNCATE TABLE IF EXISTS system.{table}"],
            check=False)  # a table missing (older/newer ClickHouse) isn't fatal
    print(f"truncated: {', '.join(TABLES)}")


if __name__ == "__main__":
    main()
