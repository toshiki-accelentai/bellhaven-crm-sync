"""CLI: python -m bhsync {run|serve|verify|report}."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from . import apply, config, match, scrape, store
from .crm import Client
from .models import ERROR
from .verify import verify

DATA = os.path.dirname(config.DB_PATH) or "."
BASELINE = os.path.join(DATA, "baseline_accounts.json")


def _client(conn):
    return Client(config.API, config.TOKEN)


def cmd_run(conn, args):
    client = _client(conn)
    # Finish half-applied, already-approved writes first so matching never sees a CHOW without its pointer.
    for p in store.list_proposals(conn, (ERROR,)):
        if (p.get("result") or {}).get("new_id"):
            print("resume:", apply.approve(conn, p["id"], client)["message"])
    locations, meta = scrape.scrape(config.BASE)
    complete, reasons = scrape.gate(locations, meta, store.last_location_count(conn), config.MAX_LOCATION_DROP)
    accounts, contacts = client.accounts(), client.contacts()
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(BASELINE):  # first run = the "before" picture that verify compares against
        _dump(BASELINE, accounts)
    _dump(os.path.join(DATA, "last_scrape.json"), [l.to_dict() for l in locations])
    proposals, report, warnings = match.build(locations, accounts, contacts, site_complete=complete)
    warnings = reasons + meta.warnings + warnings
    inserted, suppressed = store.upsert_proposals(conn, proposals)
    store.log_run(conn, len(locations), inserted, suppressed, warnings)
    _dump(os.path.join(DATA, "last_report.json"), report)
    print(f"{len(locations)} locations, {len(accounts)} accounts -> {len(proposals)} findings: "
          f"{inserted} new proposals, {suppressed} already proposed/decided")
    print("classes:", dict(Counter(r["class"] for r in report)))
    for w in warnings:
        print("WARN", w)


def cmd_report(conn, args):
    for r in json.load(open(os.path.join(DATA, "last_report.json"))):
        print(f"{r['class']:<28} {r['name']:<50} {r['account_id']:<20} {r['rule']}")
    print(dict(Counter(p["status"] for p in store.list_proposals(conn))))


def cmd_serve(conn, args):
    from .app import serve
    serve(conn, _client(conn), dry_run=args.dry_run, port=args.port)


def cmd_verify(conn, args):
    ok, lines = verify(json.load(open(BASELINE)), _client(conn).accounts(), store.list_proposals(conn))
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)


def main():
    ap = argparse.ArgumentParser(prog="bhsync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="scrape + match + queue proposals (never writes to the CRM)")
    sub.add_parser("report", help="print the last classification report")
    s = sub.add_parser("serve", help="review app on 127.0.0.1")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--port", type=int, default=5000)
    sub.add_parser("verify", help="prove every CRM change since the first run was approved")
    args = ap.parse_args()
    conn = store.connect(config.DB_PATH)
    {"run": cmd_run, "report": cmd_report, "serve": cmd_serve, "verify": cmd_verify}[args.cmd](conn, args)


if __name__ == "__main__":
    main()
