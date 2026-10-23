"""Export the analytics data as Parquet files for DuckDB (R57; analytics_export.py says what is in them).

    docker compose exec webapp python export_analytics.py --out /tmp/export
    docker compose cp webapp:/tmp/export ./analytics-export      # then: cd analytics-export && duckdb -init explore.sql

--include-text adds what people wrote. On the EC2 server, Analytics → Download export gives the same files.

Personal text (questions, answers, comments) is left out unless --include-text.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "/app/dags")   # as web_api does: the shared code (common) in the web app container

import analytics_export  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the folder to write the files to (created if needed)")
    parser.add_argument("--include-text", action="store_true",
                        help="also export what people wrote: questions, answers, feedback comments, gap items")
    args = parser.parse_args(argv)
    manifest = analytics_export.export(Path(args.out), include_text=args.include_text)
    for name, table in manifest["tables"].items():
        print(f"{name}.parquet  {table['rows']} rows")
    if manifest["skipped"]:
        print(f"not created on this install yet: {', '.join(manifest['skipped'])}", file=sys.stderr)
    print(f"Wrote {args.out}; open it with: cd {args.out} && duckdb -init explore.sql")
    return 0


if __name__ == "__main__":
    sys.exit(main())
