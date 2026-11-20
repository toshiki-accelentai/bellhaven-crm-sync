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
LABELS = {"CHOW": "CHOW", "REPARENT": "Re-parent", "DUPLICATE": "Duplicate", "STALE": "Not on website",
          "NEW": "New account", "UPDATE": "Field fix", "COSMETIC": "Name tidy-up"}
_APPROVE_LOCK = threading.Lock()  # process-level: one CRM write at a time


def _money(v) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return "-"


def _touched(p: dict) -> list[str]:
    new_id = (p.get("result") or {}).get("new_id")
    ids = ([new_id] if new_id else []) + [d["account_id"] for d in p["diff"]]
    return list(dict.fromkeys(i for i in ids if i and i != "$new"))


def create_app(conn, client=None, dry_run: bool = False, approve_fn=None) -> Flask:
    app = Flask(__name__)  # Jinja autoescape stays on for .html templates
    app.secret_key = os.urandom(24)
    app.jinja_env.filters.update(money=_money, pretty=lambda o: json.dumps(o, indent=2, ensure_ascii=False))
    app.jinja_env.globals["crm"] = lambda i: config.CRM_URL.format(id=i)
    app.jinja_env.globals["safe_http"] = lambda u: isinstance(u, str) and u.startswith(("http://", "https://"))
    app.jinja_env.globals["touched"] = _touched

    app.jinja_env.globals["link"] = lambda **kw: url_for("index", **{k: v for k, v in kw.items() if v})

    @app.get("/")
    def index():
        view = "decided" if request.args.get("view") == "decided" else "pending"
        kind = request.args.get("kind") if request.args.get("kind") in KINDS else ""
        everything = store.list_proposals(conn)
        in_view = [p for p in everything if p["status"] in VIEWS[view]]
        shown = [p for p in in_view if not kind or p["kind"] == kind]
        sel = request.args.get("id", type=int)
        # The selected proposal may have just left this view (e.g. approved): keep showing it with its outcome.
        selected = next((p for p in everything if p["id"] == sel), None) or (shown[0] if shown else None)
        # "Next" follows list order (risk, id), even when the selected item has just left the list.
        pos = (KINDS.index(selected["kind"]), selected["id"]) if selected else (-1, -1)
        after = [p["id"] for p in shown if (KINDS.index(p["kind"]), p["id"]) > pos] or [p["id"] for p in shown if p is not selected
                                                                           and (not selected or p["id"] != selected["id"])]
        return render_template(
            "index.html", view=view, kind_filter=kind, dry_run=dry_run, approvable=APPROVABLE,
            shown=shown, selected=selected, next_id=after[0] if after else None,
            status_counts=Counter(p["status"] for p in everything), view_total=len(in_view),
            view_counts=Counter(p["kind"] for p in in_view), kinds=KINDS, labels=LABELS)

    def _back(pid):
        return redirect(url_for("index", **{k: v for k, v in {
            "view": request.form.get("view"), "kind": request.form.get("kind"), "id": pid}.items() if v}))

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
        return _back(pid)

    @app.post("/proposals/<int:pid>/reject")
    def reject(pid):
        p = store.get(conn, pid)
        if p is None or p["status"] not in APPROVABLE + ("error",):
            flash(f"Proposal {pid} cannot be rejected.", "error")
        else:
            store.reject(conn, pid, request.form.get("reason", "")[:500])
            flash(f"Proposal {pid} rejected.", "ok")
        return _back(pid)

    return app


def serve(conn, client, dry_run: bool = False, port: int = 5000) -> None:
    create_app(conn, client, dry_run).run(host="127.0.0.1", port=port)
