from __future__ import annotations

import csv
import io
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import requests


def load_local_env(env_path: Path | None = None) -> None:
    """Load a local .env file without overriding existing OS variables.

    By default looks at support_tickets/.env (the parent directory of this
    package), so the agent shares credentials with the rest of the project.
    """
    candidate = env_path or (Path(__file__).resolve().parent.parent / ".env")
    if not candidate.exists():
        return

    for raw_line in candidate.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()


class SupersetClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        provider: str = "db",
        verify_ssl: bool = True,
        timeout_seconds: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.provider = provider
        self.verify_ssl = verify_ssl
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.access_token: str | None = None
        self.csrf_token: str | None = None

    def login(self) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/v1/security/login",
            json={
                "username": self.username,
                "password": self.password,
                "provider": self.provider,
                "refresh": True,
            },
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        return payload

    def fetch_csrf_token(self) -> str:
        response = self.session.get(
            f"{self.base_url}/api/v1/security/csrf_token/",
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        payload = response.json()
        self.csrf_token = payload["result"]
        self.session.headers.update({"X-CSRFToken": self.csrf_token})
        return self.csrf_token

    def authenticate(self) -> None:
        self.login()
        self.fetch_csrf_token()

    def authenticate_login_only(self) -> dict[str, Any]:
        return self.login()

    def get_current_user(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/v1/me/",
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def get_openapi_spec(self, version: str = "v1") -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/{version}/_openapi",
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def list_databases(self, page_size: int = 100) -> dict[str, Any]:
        query = f"(page:0,page_size:{page_size})"
        response = self.session.get(
            f"{self.base_url}/api/v1/database/",
            params={"q": query},
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def execute_sql(
        self,
        sql: str,
        database_id: int,
        schema: str | None = None,
        catalog: str | None = None,
        query_limit: int = 1000,
        poll_seconds: float = 1.5,
        max_wait_seconds: int = 120,
    ) -> dict[str, Any]:
        client_id = self._generate_client_id()
        payload: dict[str, Any] = {
            "database_id": database_id,
            "sql": sql,
            "schema": schema,
            "catalog": catalog,
            "queryLimit": query_limit,
            "runAsync": False,
            "select_as_cta": False,
            "expand_data": True,
            "client_id": client_id,
            "tab": "payouts_agent",
        }

        response = self.session.post(
            f"{self.base_url}/api/v1/sqllab/execute/",
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        if not response.ok:
            raise RuntimeError(
                f"Superset SQL execute failed: HTTP {response.status_code}\n{response.text}"
            )
        result = response.json()

        data_rows = self._extract_rows(result)
        if data_rows is not None:
            return {
                "mode": "sync",
                "client_id": client_id,
                "raw_response": result,
                "rows": data_rows,
            }

        query_id = self._extract_query_id(result)
        if query_id is None:
            return {
                "mode": "unknown",
                "client_id": client_id,
                "raw_response": result,
                "rows": [],
            }

        final_query = self._wait_for_query(query_id, poll_seconds, max_wait_seconds)
        state = str(final_query.get("result", {}).get("state") or final_query.get("state") or "").lower()
        if state not in {"success", "done"}:
            raise RuntimeError(json.dumps(final_query, ensure_ascii=False, indent=2))

        exported_rows = self._export_csv_rows(client_id)
        return {
            "mode": "async",
            "client_id": client_id,
            "query_id": query_id,
            "query": final_query,
            "rows": exported_rows,
        }

    def _wait_for_query(
        self,
        query_id: int,
        poll_seconds: float,
        max_wait_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            response = self.session.get(
                f"{self.base_url}/api/v1/query/{query_id}",
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            payload = response.json()
            state = str(payload.get("result", {}).get("state") or payload.get("state") or "").lower()
            if state in {"success", "done", "failed", "error", "stopped"}:
                return payload
            time.sleep(poll_seconds)
        raise TimeoutError(f"Superset query {query_id} did not finish within {max_wait_seconds} seconds")

    def _export_csv_rows(self, client_id: str) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/api/v1/sqllab/export/{client_id}/",
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        return list(reader)

    @staticmethod
    def _extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("data"), list):
            return payload["result"]["data"]
        if isinstance(payload.get("result"), list):
            return payload["result"]
        return None

    @staticmethod
    def _extract_query_id(payload: dict[str, Any]) -> int | None:
        candidates = [
            payload.get("query", {}).get("id") if isinstance(payload.get("query"), dict) else None,
            payload.get("result", {}).get("query", {}).get("id")
            if isinstance(payload.get("result"), dict) and isinstance(payload.get("result", {}).get("query"), dict)
            else None,
            payload.get("query_id"),
            payload.get("result", {}).get("query_id") if isinstance(payload.get("result"), dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, int):
                return candidate
        return None

    @staticmethod
    def _generate_client_id() -> str:
        return f"pa{secrets.token_hex(4)}"[:11]


def build_client_from_env() -> SupersetClient:
    required_vars = ["SUPERSET_URL", "SUPERSET_USERNAME", "SUPERSET_PASSWORD"]
    missing = [name for name in required_vars if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    verify_ssl = os.environ.get("SUPERSET_VERIFY_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}
    timeout_seconds = int(os.environ.get("SUPERSET_TIMEOUT_SECONDS", "60"))

    return SupersetClient(
        base_url=os.environ["SUPERSET_URL"],
        username=os.environ["SUPERSET_USERNAME"],
        password=os.environ["SUPERSET_PASSWORD"],
        provider=os.environ.get("SUPERSET_PROVIDER", "db"),
        verify_ssl=verify_ssl,
        timeout_seconds=timeout_seconds,
    )
