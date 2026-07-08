"""CLI: init-db | run | report | serve | reseed"""
from __future__ import annotations

import argparse
import subprocess
import sys

from . import config


def main() -> None:
    p = argparse.ArgumentParser(prog="adwatch", description="Ad-activity monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="Create the database and seed companies")
    sub.add_parser("run", help="Run one collection cycle (collect -> classify -> store)")
    sub.add_parser("report", help="Generate the PDF report from stored data")
    sub.add_parser("reseed", help="Reset the company list from config/companies.yaml")
    serve = sub.add_parser("serve", help="Start the Streamlit dashboard")
    serve.add_argument("--port", type=int, default=8501)

    args = p.parse_args()

    if args.cmd == "init-db":
        from .collect.pipeline import seed_companies_if_empty
        from .db import init_db
        init_db()
        n = seed_companies_if_empty()
        print(f"DB ready at {config.DB_URL} · seeded {n} companies")

    elif args.cmd == "run":
        from .collect.pipeline import run_once
        summary = run_once()
        print("Run complete:", summary)

    elif args.cmd == "report":
        from .report import build_report
        path = build_report()
        print("Report written:", path)

    elif args.cmd == "reseed":
        from .collect.pipeline import reseed_from_file
        print("Re-seeded companies:", reseed_from_file())

    elif args.cmd == "serve":
        print(f"Dashboard: http://localhost:{args.port}   (mode={config.MODE})")
        subprocess.run([sys.executable, "-m", "streamlit", "run", "streamlit_app.py",
                        "--server.port", str(args.port),
                        "--browser.gatherUsageStats", "false"],
                       cwd=str(config.ROOT), check=False)


if __name__ == "__main__":
    main()
