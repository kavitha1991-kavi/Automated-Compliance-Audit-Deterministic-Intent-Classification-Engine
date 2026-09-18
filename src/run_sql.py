"""
Run any .sql file in sql/ against the project database and print the
result set. Useful for graders/reviewers checking each analytical query
without opening a separate SQL client.

    python src/run_sql.py sql/01_daily_compliance_breach_summary.sql
    python src/run_sql.py sql/11_pii_assurance_check.sql --database-url postgresql+psycopg2://...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db import get_engine


def strip_leading_comments(sql: str) -> str:
    lines = sql.splitlines()
    out, in_header = [], True
    for line in lines:
        if in_header and (line.strip().startswith("--") or line.strip() == ""):
            continue
        in_header = False
        out.append(line)
    return "\n".join(out)


def run(sql_path: Path, database_url: str | None = None) -> pd.DataFrame:
    engine = get_engine(database_url)
    sql = sql_path.read_text(encoding="utf-8")
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        cols = list(result.keys())
    return pd.DataFrame(rows, columns=cols)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--database-url", type=str, default=None)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    df = run(args.sql_file, args.database_url)
    print(f"\n{args.sql_file.name}  ->  {len(df)} row(s)\n")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(df.head(args.limit).to_string(index=False))
