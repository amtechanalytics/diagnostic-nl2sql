"""
execute_sql.py
==============
Loads the synthetic warehouse into an in-process DuckDB database and executes
agent-generated SQL with guardrails. Used by the lineage_exec arm's tool loop
and by score.py for ground-truth checks.

Guardrails:
  - read-only: reject anything that isn't a single SELECT / WITH...SELECT
  - row cap: truncate results to MAX_ROWS to bound tokens fed back to the model
  - safe failure: SQL errors are returned as structured text, not exceptions,
    so the agent can see the error and self-correct.

Requires: duckdb (pip install duckdb). The DDL files in ../warehouse build the
raw->stage->transform->dp layers from the generated CSVs.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WH = os.path.join(HERE, "..", "warehouse")

MAX_ROWS = 60          # cap rows returned to the model
DDL_FILES = ["01_raw_ddl.sql", "02_stage_ddl.sql",
             "03_transform_ddl.sql", "04_dp_views.sql"]

_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|copy|"
    r"pragma|install|load|export|replace)\b", re.IGNORECASE)


def build_database(db_path=":memory:"):
    """Build a fresh warehouse DB from the DDL files. Returns a duckdb conn.
    Run from the warehouse/ dir (the COPY paths in 01_raw_ddl.sql are relative
    to warehouse/), so we chdir there during build."""
    import duckdb
    con = duckdb.connect(db_path)
    cwd = os.getcwd()
    try:
        os.chdir(WH)
        for fn in DDL_FILES:
            with open(os.path.join(WH, fn)) as f:
                con.execute(f.read())
    finally:
        os.chdir(cwd)
    return con


def _is_read_only(sql):
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty query"
    # allow a leading WITH ... then SELECT; reject any write keyword
    if _WRITE_RE.search(s):
        # allow 'create' only if it's not actually present as a statement verb;
        # simplest: reject outright -- agents should only SELECT here.
        return False, "only read-only SELECT/WITH queries are permitted"
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "query must start with SELECT or WITH"
    return True, ""


def run_sql(con, sql, max_rows=MAX_ROWS):
    """Execute one read-only SQL statement. Returns a dict:
       {ok, columns, rows, row_count, truncated, error}"""
    ok, why = _is_read_only(sql)
    if not ok:
        return {"ok": False, "error": f"rejected: {why}", "columns": [],
                "rows": [], "row_count": 0, "truncated": False}
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        all_rows = cur.fetchall()
        truncated = len(all_rows) > max_rows
        rows = all_rows[:max_rows]
        # make rows JSON-friendly
        rows = [[(round(v, 4) if isinstance(v, float) else v) for v in r]
                for r in rows]
        return {"ok": True, "error": None, "columns": cols, "rows": rows,
                "row_count": len(all_rows), "truncated": truncated}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "columns": [], "rows": [], "row_count": 0, "truncated": False}


def format_result_for_model(res):
    """Compact text rendering of a result for feeding back to the model."""
    if not res["ok"]:
        return f"SQL ERROR: {res['error']}"
    if res["row_count"] == 0:
        return "OK: 0 rows."
    head = " | ".join(res["columns"])
    lines = [head, "-" * len(head)]
    for r in res["rows"]:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    tail = (f"\n[{res['row_count']} rows total"
            + (", truncated to %d]" % len(res["rows"]) if res["truncated"] else "]"))
    return "\n".join(lines) + tail


if __name__ == "__main__":
    # smoke test (requires duckdb + generated CSVs)
    con = build_database()
    for q in [
        "SELECT species, SUM(units) u FROM dp.sales_by_species_channel_month "
        "WHERE month='2025-01' GROUP BY 1 ORDER BY u DESC",
        "SELECT month, SUM(units) FROM xform.fct_sales s JOIN xform.dim_product p "
        "ON s.sku_id=p.sku_id WHERE p.species='cattle' AND p.family='vaccine' "
        "AND month BETWEEN '2025-05' AND '2025-09' GROUP BY 1 ORDER BY 1",
        "DROP TABLE xform.fct_sales",  # should be rejected
    ]:
        print(">>", q[:70])
        print(format_result_for_model(run_sql(con, q)))
        print()
