"""CLI: init-db | run | report | serve | reseed"""
from __future__ import annotations

import argparse

from . import config


def main() -> None:
    p = argparse.ArgumentParser(prog="adwatch", description="Ad-activity monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="Create the database and seed companies")
    sub.add_parser("run", help="Run one collection cycle (collect -> classify -> store)")
    sub.add_parser("report", help="Generate the PDF report from stored data")
    sub.add_parser("reseed", help="Reset the company list from config/companies.yaml")
    serve = sub.add_parser("serve", help="Start the local dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = p.parse_args()

    if args.cmd == "init-db":
        from .db import init_db
        from .pipeline import seed_companies_if_empty
        init_db()
        n = seed_companies_if_empty()
        print(f"DB ready at {config.DB_URL} · seeded {n} companies")

    elif args.cmd == "run":
        from .pipeline import run_once
        summary = run_once()
        print("Run complete:", summary)

    elif args.cmd == "report":
        from .report import build_report
        path = build_report()
        print("Report written:", path)

    elif args.cmd == "reseed":
        from .pipeline import reseed_from_file
        print("Re-seeded companies:", reseed_from_file())

    elif args.cmd == "serve":
        import uvicorn
        print(f"Dashboard: http://{args.host}:{args.port}   (mode={config.MODE})")
        uvicorn.run("adwatch.web:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
