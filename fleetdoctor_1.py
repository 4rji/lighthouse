#!/usr/bin/env python3
"""OOB Fleet Doctor: read-only Lighthouse node inventory."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class LighthouseError(RuntimeError):
    pass


def load_env_file() -> Path | None:
    """Load the first available .env file without executing shell code."""
    configured = os.getenv("FLEETDOCTOR_ENV_FILE")
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent / ".env",
        ]
    )

    for path in candidates:
        if not path.is_file():
            continue

        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                print(
                    f"ADVERTENCIA: se ignoró {path}:{line_number}; "
                    "se esperaba NOMBRE=valor.",
                    file=sys.stderr,
                )
                continue

            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]

            if name:
                os.environ.setdefault(name, value)

        try:
            if path.stat().st_mode & 0o077:
                print(
                    f"ADVERTENCIA: {path} puede ser leído por otros usuarios. "
                    f"Ejecute: chmod 600 {path}",
                    file=sys.stderr,
                )
        except OSError:
            pass
        return path

    return None


class LighthouseClient:
    def __init__(
        self,
        lighthouse: str,
        api_version: str,
        verify_tls: bool,
        timeout: int,
    ) -> None:
        self.base_url = (
            f"{lighthouse.rstrip('/')}/api/{api_version.strip('/')}"
        )
        self.timeout = timeout
        self.token: str | None = None
        self.ssl_context = (
            ssl.create_default_context()
            if verify_tls
            else ssl._create_unverified_context()
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"

        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = raw or exc.reason
            raise LighthouseError(
                f"HTTP {exc.code} en {endpoint}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LighthouseError(
                f"No fue posible conectar con {url}: {exc.reason}"
            ) from exc

    def login(self, username: str, password: str) -> None:
        result = self._request(
            "POST",
            "sessions",
            {"username": username, "password": password},
        )
        state = str(result.get("state", "")).lower()
        token = result.get("session")
        if state == "failed":
            raise LighthouseError(
                "Lighthouse rechazó la autenticación. Compruebe que el usuario "
                "esté habilitado, que use contraseña local y que la contraseña "
                "sea correcta."
            )
        if not token:
            raise LighthouseError(
                f"Lighthouse no completó la sesión (estado: "
                f"{result.get('state', 'desconocido')})."
            )
        self.token = str(token)

    def logout(self) -> None:
        if not self.token:
            return
        try:
            self._request("DELETE", "sessions/self")
        finally:
            self.token = None

    def get_all_nodes(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        page = 1

        while True:
            result = self._request(
                "GET",
                "nodes",
                query={"page": page, "per_page": 250},
            )
            nodes.extend(result.get("nodes", []))
            meta = result.get("meta", {})
            total_pages = int(meta.get("total_pages", 1) or 1)
            if page >= total_pages:
                break
            page += 1

        return nodes


def diagnose(node: dict[str, Any]) -> tuple[str, str]:
    runtime = node.get("runtime_status") or {}
    connection = str(runtime.get("connection_status", "unknown")).lower()
    action_status = str(runtime.get("action_status") or "").lower()
    action_error = str(runtime.get("action_error_message") or "").strip()
    approved = bool(node.get("approved"))
    enrollment = str(node.get("status", "Unknown"))

    if action_status == "error" or action_error:
        return "CRITICAL", action_error or "La última acción terminó con error"
    if connection in {"disconnected", "never seen"}:
        return "CRITICAL", f"VPN {connection}"
    if not approved or enrollment in {"Pending", "Registering"}:
        return "WARNING", f"Inscripción {enrollment}"
    if connection in {"pending", "unknown"}:
        return "WARNING", f"Conexión VPN {connection}"
    if connection == "connected":
        return "HEALTHY", "VPN conectada"
    return "UNKNOWN", f"Estado no reconocido: {connection}"


def normalized_node(node: dict[str, Any]) -> dict[str, Any]:
    runtime = node.get("runtime_status") or {}
    severity, diagnosis = diagnose(node)
    return {
        "id": node.get("id"),
        "name": node.get("name") or "(sin nombre)",
        "model": node.get("model") or node.get("product") or "-",
        "firmware": node.get("firmware_version") or "-",
        "enrollment": node.get("status") or "Unknown",
        "connection": runtime.get("connection_status") or "unknown",
        "severity": severity,
        "diagnosis": diagnosis,
    }


def print_report(nodes: list[dict[str, Any]]) -> None:
    normalized = [normalized_node(node) for node in nodes]
    priority = {"CRITICAL": 0, "WARNING": 1, "UNKNOWN": 2, "HEALTHY": 3}
    normalized.sort(
        key=lambda item: (
            priority.get(str(item["severity"]), 9),
            str(item["name"]).lower(),
        )
    )

    totals: dict[str, int] = {}
    for node in normalized:
        severity = str(node["severity"])
        totals[severity] = totals.get(severity, 0) + 1

    print("\nOOB Fleet Doctor — resumen de solo lectura")
    print("=" * 72)
    print(
        f"Nodos: {len(normalized)} | "
        f"Saludables: {totals.get('HEALTHY', 0)} | "
        f"Advertencias: {totals.get('WARNING', 0)} | "
        f"Críticos: {totals.get('CRITICAL', 0)} | "
        f"Desconocidos: {totals.get('UNKNOWN', 0)}"
    )
    print()
    print(
        f"{'SEVERIDAD':<11} {'NODO':<24} {'MODELO':<15} "
        f"{'VPN':<13} DIAGNÓSTICO"
    )
    print("-" * 100)
    for node in normalized:
        print(
            f"{str(node['severity']):<11} "
            f"{str(node['name'])[:23]:<24} "
            f"{str(node['model'])[:14]:<15} "
            f"{str(node['connection'])[:12]:<13} "
            f"{node['diagnosis']}"
        )


def env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventario y diagnóstico inicial de Lighthouse en modo lectura."
    )
    parser.add_argument(
        "lighthouse",
        nargs="?",
        default=os.getenv("FLEETDOCTOR_URL"),
        help=(
            "URL de Lighthouse. También puede definirse con FLEETDOCTOR_URL."
        ),
    )
    parser.add_argument(
        "--username",
        default=os.getenv("FLEETDOCTOR_USERNAME", "fleetdoctor"),
    )
    parser.add_argument(
        "--api-version",
        default=os.getenv("FLEETDOCTOR_API_VERSION", "v3.7"),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("FLEETDOCTOR_TIMEOUT", "30")),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=env_enabled("FLEETDOCTOR_INSECURE"),
        help="Permitir certificado TLS autofirmado (solo laboratorio).",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Guardar además la respuesta de nodos en un archivo JSON.",
    )
    args = parser.parse_args()
    if not args.lighthouse:
        parser.error(
            "indique la URL de Lighthouse o defina FLEETDOCTOR_URL"
        )
    return args


def main() -> int:
    env_file = load_env_file()
    args = parse_args()
    password = os.getenv("FLEETDOCTOR_PASSWORD")
    if password is None:
        password = getpass.getpass(
            f"Contraseña para {args.username} en {args.lighthouse}: "
        )
    elif not password:
        print(
            "ERROR: FLEETDOCTOR_PASSWORD está vacío.",
            file=sys.stderr,
        )
        return 2

    client = LighthouseClient(
        lighthouse=args.lighthouse,
        api_version=args.api_version,
        verify_tls=not args.insecure,
        timeout=args.timeout,
    )

    try:
        client.login(args.username, password)
        del password
        nodes = client.get_all_nodes()
        if env_file:
            print(f"Configuración cargada desde: {env_file}")
        print_report(nodes)

        if args.json_output:
            args.json_output.write_text(
                json.dumps(
                    {"nodes": nodes},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"\nJSON guardado en: {args.json_output}")
        return 0
    except LighthouseError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        if not args.insecure:
            print(
                "Si Lighthouse usa un certificado autofirmado, repita la prueba "
                "con --insecure.",
                file=sys.stderr,
            )
        return 1
    finally:
        try:
            client.logout()
        except LighthouseError as exc:
            print(
                f"ADVERTENCIA: no se pudo cerrar la sesión: {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())

