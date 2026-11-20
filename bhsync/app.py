"""Local review UI: shows each proposal with its evidence; nothing writes to the CRM without a click."""
from __future__ import annotations

import json
import os
import threading
from collections import Counter

from flask import Flask, flash, redirect, render_template, request, url_for

from . import config, store
from .models import KINDS, NEEDS_REAPPROVAL, PENDING

VIEWS = {"pending": (PENDING, NEEDS_REAPPROVAL, "error"),
         "decided": ("applied", "rejected", "superseded")}
APPROVABLE = (PENDING, NEEDS_REAPPROVAL, "error")  # error = resume a partially applied write
_APPROVE_LOCK = threading.Lock()  # process-level: one CRM write at a time


def _money(v) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return "-"


def _touched(p: dict) -> list[str]:
    extra = [str(v) for k, v in (p.get("result") or {}).items() if k in ("new_id", "new_account_id", "created_id")]
    ids = [d["account_id"] for d in p["diff"]] + extra
    return list(dict.fromkeys(i for i in ids if i and i != "$new"))


def create_app(conn, client=None, dry_run: bool = False, approve_fn=None) -> Flask:
    app = Flask(__name__)  # Jinja autoescape stays on for .html templates
    app.secret_key = os.urandom(24)
    app.jinja_env.filters.update(money=_money, pretty=lambda o: json.dumps(o, indent=2, ensure_ascii=False))
    app.jinja_env.globals["crm"] = lambda i: config.CRM_URL.format(id=i)
    app.jinja_env.globals["safe_http"] = lambda u: isinstance(u, str) and u.startswith(("http://", "https://"))
    app.jinja_env.globals["touched"] = _touched

    @app.get("/")
    def index():
        view = "decided" if request.args.get("view") == "decided" else "pending"
        everything = store.list_proposals(conn)
        return render_template(
            "index.html", view=view, dry_run=dry_run, approvable=APPROVABLE,
            proposals=[p for p in everything if p["status"] in VIEWS[view]],
            status_counts=Counter(p["status"] for p in everything),
            kind_counts=Counter(p["kind"] for p in everything), kinds=KINDS)

    @app.post("/proposals/<int:pid>/approve")
    def approve(pid):
        fn = approve_fn
        if fn is None:
            from . import apply  # lazy: module is written separately
            fn = apply.approve
        with _APPROVE_LOCK:
            p = store.get(conn, pid)  # re-read inside the lock so a double-submit sees the new status
            if p is None or p["status"] not in APPROVABLE:
                flash(f"Proposal {pid} cannot be approved (status: {p['status'] if p else 'missing'}).", "error")
            else:
                try:
                    r = fn(conn, pid, client, dry_run)
                    flash(r.get("message") or r.get("status", ""), "ok" if r.get("ok") else "error")
                except Exception as e:  # surface, never swallow
                    flash(f"Approve failed: {e}", "error")
        return redirect(f"{url_for('index')}#p{pid}")

    @app.post("/proposals/<int:pid>/reject")
    def reject(pid):
        p = store.get(conn, pid)
        if p is None or p["status"] not in APPROVABLE + ("error",):
            flash(f"Proposal {pid} cannot be rejected.", "error")
        else:
            store.reject(conn, pid, request.form.get("reason", "")[:500])
            flash(f"Proposal {pid} rejected.", "ok")
        return redirect(f"{url_for('index')}#p{pid}")

    return app


def serve(conn, client, dry_run: bool = False, port: int = 5000) -> None:
    create_app(conn, client, dry_run).run(host="127.0.0.1", port=port)
