"""Thin CRM API client. Reads retry on transient errors; POST never retries blindly."""
from __future__ import annotations

import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
PAGE_SIZE = 50
MAX_PAGES = 100


class CRMError(Exception):
    def __init__(self, message: str, status: int | None = None, body=None):
        super().__init__(message)
        self.status, self.body = status, body


class Client:
    def __init__(self, api_base: str, token: str, audit=None, timeout: int = 20):
        if not token:
            raise CRMError("BH_TOKEN is not set (see .env.example)")
        self.api = api_base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "bhsync/1.0"})
        self.timeout = timeout
        self.audit = audit  # callable(method, url, request, status, response) | None

    def _call(self, method: str, path: str, *, params=None, json=None, retries: int = 3):
        url = f"{self.api}{path}"
        for attempt in range(retries + 1):
            try:
                r = self.s.request(method, url, params=params, json=json, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise CRMError(f"{method} {path}: {e}") from e
            if r.status_code in RETRY_STATUS and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            if self.audit and method != "GET":
                self.audit(method, url, json, r.status_code, body)
            if r.status_code >= 400:
                raise CRMError(f"{method} {path} -> HTTP {r.status_code}: {str(body)[:300]}", r.status_code, body)
            return body
        raise CRMError(f"{method} {path}: retries exhausted")

    def _paged(self, path: str, **params) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._call("GET", path, params={**params, "page": page, "page_size": PAGE_SIZE})
            rows += body["data"]
            if not body["data"] or len(rows) >= body["total"]:
                return rows
        raise CRMError(f"{path}: more than {MAX_PAGES} pages")

    def accounts(self) -> list[dict]:
        return self._paged("/accounts")

    def contacts(self) -> list[dict]:
        return self._paged("/contacts")

    def find_accounts(self, **filters) -> list[dict]:
        return self._paged("/accounts", **filters)

    def get_account(self, account_id: str) -> dict:
        return _unwrap(self._call("GET", f"/accounts/{account_id}"))

    def patch_account(self, account_id: str, fields: dict) -> dict:
        # PATCH with absolute values is idempotent, so transient retries are safe.
        return _unwrap(self._call("PATCH", f"/accounts/{account_id}", json=fields))

    def create_account(self, fields: dict) -> dict:
        # Never retried: a timeout may still have created the account; caller reconciles.
        return _unwrap(self._call("POST", "/accounts", json=fields, retries=0))


def _unwrap(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body
