#!/usr/bin/env python3
"""Live, read-only terminal dashboard for Opengear Lighthouse nodes."""

from __future__ import annotations

import argparse
import curses
import getpass
import math
import os
import sys
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fleetdoctor import (
    LighthouseClient,
    LighthouseError,
    env_enabled,
    load_env_file,
)


PAIR_CYAN = 1
PAIR_GREEN = 2
PAIR_YELLOW = 3
PAIR_RED = 4
PAIR_MAGENTA = 5
PAIR_WHITE = 6
PAIR_DIM = 7

SEVERITY_PRIORITY = {
    "CRITICAL": 0,
    "WARNING": 1,
    "UNKNOWN": 2,
    "HEALTHY": 3,
}

SEVERITY_COLOR = {
    "HEALTHY": PAIR_GREEN,
    "WARNING": PAIR_YELLOW,
    "CRITICAL": PAIR_RED,
    "UNKNOWN": PAIR_MAGENTA,
}


def text(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def english_diagnosis(node: dict[str, Any]) -> tuple[str, str]:
    runtime = node.get("runtime_status") or {}
    connection = str(runtime.get("connection_status") or "unknown").lower()
    action_status = str(runtime.get("action_status") or "").lower()
    action_error = str(runtime.get("action_error_message") or "").strip()
    enrollment = str(node.get("status") or "Unknown")
    profile = node.get("profile") or {}
    profile_commit = str(profile.get("last_commit_status") or "").lower()
    cell = node.get("cellhealth_runtime_status") or {}
    cell_status = str(cell.get("status") or "unknown").lower()

    if action_status == "error" or action_error:
        return "CRITICAL", action_error or "Last Lighthouse action failed"
    if connection in {"disconnected", "never seen"}:
        return "CRITICAL", f"Node VPN is {connection}"
    if not bool(node.get("approved")):
        return "WARNING", "Node is awaiting approval"
    if enrollment in {"Pending", "Registering", "Registered"}:
        return "WARNING", f"Enrollment is {enrollment.lower()}"
    if connection in {"pending", "unknown"}:
        return "WARNING", f"VPN state is {connection}"
    if profile_commit == "failed":
        return "WARNING", "Last profile commit failed"
    if cell_status in {
        "bad",
        "sim_issues",
        "connectivity_test_failed",
        "interface_disabled",
    }:
        return "WARNING", f"Cellular health is {cell_status.replace('_', ' ')}"
    if connection == "connected":
        return "HEALTHY", "VPN connected; no active API fault"
    return "UNKNOWN", f"Unrecognized VPN state: {connection}"


def node_view(node: dict[str, Any]) -> dict[str, Any]:
    runtime = node.get("runtime_status") or {}
    severity, diagnosis = english_diagnosis(node)
    interfaces = node.get("interfaces") or []
    ipv4_addresses = [
        str(interface.get("ipv4_addr"))
        for interface in interfaces
        if interface.get("ipv4_addr")
    ]
    ports = node.get("ports") or []
    configured_ports = [
        port for port in ports if str(port.get("mode") or "") != "disabled"
    ]
    active_sessions = sum(
        1 for port in ports if port.get("serial_sessions")
    )
    cell = node.get("cellhealth_runtime_status") or {}
    failover = node.get("failover_runtime_status") or {}
    profile = node.get("profile") or {}
    subscription = node.get("subscription") or {}

    return {
        "id": text(node.get("id")),
        "name": text(node.get("name"), "(unnamed node)"),
        "model": text(node.get("model") or node.get("product")),
        "firmware": text(node.get("firmware_version")),
        "serial": text(node.get("serial_number")),
        "enrollment": text(node.get("status"), "Unknown"),
        "connection": text(runtime.get("connection_status"), "unknown"),
        "last_change": format_epoch(runtime.get("change_time")),
        "address": ", ".join(ipv4_addresses[:2]) or "—",
        "interface_count": len(interfaces),
        "configured_ports": len(configured_ports),
        "total_ports": len(ports),
        "active_sessions": active_sessions,
        "cell": text(cell.get("status"), "not available"),
        "failover": text(failover.get("type"), "not available").replace("_", " "),
        "profile": text(profile.get("name"), "not assigned"),
        "profile_commit": text(profile.get("last_commit_status"), "n/a"),
        "subscription": text(subscription.get("tier"), "n/a"),
        "severity": severity,
        "diagnosis": diagnosis,
    }


def format_epoch(value: Any) -> str:
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def safe_addstr(
    screen: curses.window,
    y: int,
    x: int,
    value: str,
    style: int = 0,
    max_width: int | None = None,
) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = max(0, width - x)
    if max_width is not None:
        available = min(available, max_width)
    try:
        screen.addstr(y, x, clip(value, available), style)
    except curses.error:
        pass


def draw_box(
    screen: curses.window,
    y: int,
    x: int,
    height: int,
    width: int,
    title: str,
    color_pair: int,
) -> None:
    if height < 3 or width < 4:
        return
    style = curses.color_pair(color_pair)
    right = x + width - 1
    bottom = y + height - 1
    safe_addstr(screen, y, x, "┌" + "─" * (width - 2) + "┐", style)
    for row in range(y + 1, bottom):
        safe_addstr(screen, row, x, "│", style)
        safe_addstr(screen, row, right, "│", style)
    safe_addstr(screen, bottom, x, "└" + "─" * (width - 2) + "┘", style)
    title_text = f" {title} "
    title_x = x + max(2, (width - len(title_text)) // 2)
    safe_addstr(
        screen,
        y,
        title_x,
        clip(title_text, width - 4),
        style | curses.A_BOLD,
    )


def draw_labeled_line(
    screen: curses.window,
    y: int,
    x: int,
    width: int,
    label: str,
    value: str,
    value_pair: int = PAIR_WHITE,
) -> None:
    label_text = f"{label:<13}"
    safe_addstr(
        screen,
        y,
        x,
        label_text,
        curses.color_pair(PAIR_CYAN),
        max_width=width,
    )
    safe_addstr(
        screen,
        y,
        x + len(label_text),
        value,
        curses.color_pair(value_pair) | curses.A_BOLD,
        max_width=max(0, width - len(label_text)),
    )


class FleetDashboard:
    def __init__(
        self,
        screen: curses.window,
        client: LighthouseClient,
        lighthouse_url: str,
        api_version: str,
        refresh_seconds: int,
    ) -> None:
        self.screen = screen
        self.client = client
        self.lighthouse_url = lighthouse_url
        self.api_version = api_version
        self.refresh_seconds = max(3, refresh_seconds)
        self.nodes: list[dict[str, Any]] = []
        self.page = 0
        self.last_refresh = 0.0
        self.next_refresh = 0.0
        self.error = ""
        self.message = "Starting read-only fleet scan…"

    def run(self) -> None:
        self.configure_terminal()
        self.refresh_data()

        while True:
            now = time.monotonic()
            if now >= self.next_refresh:
                self.refresh_data()

            self.draw()
            key = self.screen.getch()
            if key in {ord("q"), ord("Q")}:
                return
            if key in {ord("r"), ord("R")}:
                self.refresh_data()
            elif key in {ord("n"), ord("N"), curses.KEY_RIGHT, curses.KEY_NPAGE}:
                self.change_page(1)
            elif key in {ord("p"), ord("P"), curses.KEY_LEFT, curses.KEY_PPAGE}:
                self.change_page(-1)

    def configure_terminal(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.nodelay(True)
        self.screen.timeout(250)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(PAIR_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(PAIR_GREEN, curses.COLOR_GREEN, -1)
            curses.init_pair(PAIR_YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(PAIR_RED, curses.COLOR_RED, -1)
            curses.init_pair(PAIR_MAGENTA, curses.COLOR_MAGENTA, -1)
            curses.init_pair(PAIR_WHITE, curses.COLOR_WHITE, -1)
            curses.init_pair(PAIR_DIM, curses.COLOR_BLUE, -1)

    def refresh_data(self) -> None:
        self.message = "Refreshing node inventory…"
        self.draw()
        try:
            raw_nodes = self.client.get_all_nodes()
            views = [node_view(node) for node in raw_nodes]
            views.sort(
                key=lambda node: (
                    SEVERITY_PRIORITY.get(str(node["severity"]), 9),
                    str(node["name"]).lower(),
                )
            )
            self.nodes = views
            self.error = ""
            self.message = f"Loaded {len(self.nodes)} nodes"
            page_count = max(1, math.ceil(len(self.nodes) / 4))
            self.page = min(self.page, page_count - 1)
        except LighthouseError as exc:
            self.error = f"API refresh failed: {exc}"
            self.message = "Showing the last successful snapshot"
        self.last_refresh = time.time()
        self.next_refresh = time.monotonic() + self.refresh_seconds

    def change_page(self, delta: int) -> None:
        page_count = max(1, math.ceil(len(self.nodes) / 4))
        self.page = (self.page + delta) % page_count

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()

        if height < 24 or width < 90:
            safe_addstr(
                self.screen,
                1,
                2,
                "OOB Fleet Doctor requires a terminal of at least 90 × 24.",
                curses.color_pair(PAIR_YELLOW) | curses.A_BOLD,
            )
            safe_addstr(
                self.screen,
                3,
                2,
                f"Current terminal: {width} × {height}",
                curses.color_pair(PAIR_WHITE),
            )
            safe_addstr(
                self.screen,
                5,
                2,
                "Resize the window, or press q to quit.",
                curses.color_pair(PAIR_CYAN),
            )
            self.screen.refresh()
            return

        self.draw_header(width)
        self.draw_summary(width)
        self.draw_node_grid(height, width)
        self.draw_footer(height, width)
        self.screen.refresh()

    def draw_header(self, width: int) -> None:
        draw_box(
            self.screen,
            0,
            0,
            3,
            width,
            "OOB FLEET DOCTOR",
            PAIR_CYAN,
        )
        host = urlparse(self.lighthouse_url).hostname or self.lighthouse_url
        updated = (
            datetime.fromtimestamp(self.last_refresh).strftime("%H:%M:%S")
            if self.last_refresh
            else "never"
        )
        line = (
            f"Lighthouse {host}  API {self.api_version}  "
            f"READ ONLY  Last refresh {updated}"
        )
        safe_addstr(
            self.screen,
            1,
            max(2, (width - len(line)) // 2),
            line,
            curses.color_pair(PAIR_WHITE) | curses.A_BOLD,
            max_width=width - 4,
        )

    def draw_summary(self, width: int) -> None:
        draw_box(
            self.screen,
            3,
            0,
            5,
            width,
            "FLEET HEALTH",
            PAIR_GREEN if not self.error else PAIR_RED,
        )
        counts = {"HEALTHY": 0, "WARNING": 0, "CRITICAL": 0, "UNKNOWN": 0}
        connected = 0
        for node in self.nodes:
            severity = str(node["severity"])
            counts[severity] = counts.get(severity, 0) + 1
            if str(node["connection"]).lower() == "connected":
                connected += 1

        summary = [
            ("TOTAL", len(self.nodes), PAIR_CYAN),
            ("HEALTHY", counts["HEALTHY"], PAIR_GREEN),
            ("WARNING", counts["WARNING"], PAIR_YELLOW),
            ("CRITICAL", counts["CRITICAL"], PAIR_RED),
            ("CONNECTED", connected, PAIR_GREEN),
        ]
        segment = max(15, (width - 4) // len(summary))
        for index, (label, value, color) in enumerate(summary):
            x = 2 + index * segment
            safe_addstr(
                self.screen,
                4,
                x,
                label,
                curses.color_pair(color) | curses.A_BOLD,
                max_width=segment - 1,
            )
            safe_addstr(
                self.screen,
                5,
                x,
                str(value),
                curses.color_pair(PAIR_WHITE) | curses.A_BOLD,
                max_width=segment - 1,
            )

        status = self.error or self.message
        status_pair = PAIR_RED if self.error else PAIR_DIM
        safe_addstr(
            self.screen,
            6,
            2,
            status,
            curses.color_pair(status_pair),
            max_width=width - 4,
        )

    def draw_node_grid(self, height: int, width: int) -> None:
        grid_y = 8
        grid_height = height - grid_y - 2
        gap = 1
        card_width = (width - gap) // 2
        card_height = (grid_height - gap) // 2
        current_nodes = self.nodes[self.page * 4 : self.page * 4 + 4]

        if not current_nodes:
            draw_box(
                self.screen,
                grid_y,
                0,
                grid_height,
                width,
                "NODES",
                PAIR_YELLOW,
            )
            safe_addstr(
                self.screen,
                grid_y + 2,
                3,
                "No nodes were returned by Lighthouse.",
                curses.color_pair(PAIR_YELLOW),
            )
            return

        positions = [
            (grid_y, 0),
            (grid_y, card_width + gap),
            (grid_y + card_height + gap, 0),
            (grid_y + card_height + gap, card_width + gap),
        ]

        for index, node in enumerate(current_nodes):
            y, x = positions[index]
            width_for_card = (
                width - x if index % 2 else card_width
            )
            height_for_card = (
                grid_y + grid_height - y if index >= 2 else card_height
            )
            self.draw_node_card(
                node,
                y,
                x,
                height_for_card,
                width_for_card,
            )

    def draw_node_card(
        self,
        node: dict[str, Any],
        y: int,
        x: int,
        height: int,
        width: int,
    ) -> None:
        severity = str(node["severity"])
        color = SEVERITY_COLOR.get(severity, PAIR_MAGENTA)
        draw_box(
            self.screen,
            y,
            x,
            height,
            width,
            str(node["name"]),
            color,
        )
        inner_x = x + 2
        inner_width = width - 4

        lines = [
            ("Health", severity, color),
            ("Model", str(node["model"]), PAIR_WHITE),
            ("Firmware", str(node["firmware"]), PAIR_WHITE),
            ("Enrollment", str(node["enrollment"]), PAIR_CYAN),
            ("VPN", str(node["connection"]), color),
            ("Address", str(node["address"]), PAIR_WHITE),
            (
                "Ports",
                f"{node['configured_ports']}/{node['total_ports']} configured"
                f"  sessions {node['active_sessions']}",
                PAIR_WHITE,
            ),
            ("Cellular", str(node["cell"]), PAIR_WHITE),
            ("Failover", str(node["failover"]), PAIR_WHITE),
            (
                "Profile",
                f"{node['profile']} / {node['profile_commit']}",
                PAIR_WHITE,
            ),
            ("Subscription", str(node["subscription"]), PAIR_WHITE),
            ("Last change", str(node["last_change"]), PAIR_WHITE),
            ("Diagnosis", str(node["diagnosis"]), color),
        ]

        available_lines = max(0, height - 2)
        for offset, (label, value, value_color) in enumerate(
            lines[:available_lines]
        ):
            draw_labeled_line(
                self.screen,
                y + 1 + offset,
                inner_x,
                inner_width,
                label,
                value,
                value_color,
            )

    def draw_footer(self, height: int, width: int) -> None:
        page_count = max(1, math.ceil(len(self.nodes) / 4))
        remaining = max(0, int(self.next_refresh - time.monotonic()))
        footer = (
            f" q Quit   r Refresh   ←/p Previous   →/n Next   "
            f"Page {self.page + 1}/{page_count}   Auto refresh {remaining}s "
        )
        safe_addstr(
            self.screen,
            height - 1,
            max(0, (width - len(footer)) // 2),
            footer,
            curses.color_pair(PAIR_CYAN) | curses.A_BOLD,
            max_width=width,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live read-only dashboard for Opengear Lighthouse nodes."
    )
    parser.add_argument(
        "lighthouse",
        nargs="?",
        default=os.getenv("FLEETDOCTOR_URL"),
        help="Lighthouse URL; defaults to FLEETDOCTOR_URL.",
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
        "--refresh",
        type=int,
        default=int(os.getenv("FLEETDOCTOR_REFRESH", "10")),
        help="Automatic refresh interval in seconds.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=env_enabled("FLEETDOCTOR_INSECURE"),
        help="Allow a self-signed TLS certificate (lab use only).",
    )
    args = parser.parse_args()
    if not args.lighthouse:
        parser.error("provide a Lighthouse URL or set FLEETDOCTOR_URL")
    return args


def main() -> int:
    load_env_file()
    args = parse_args()
    password = os.getenv("FLEETDOCTOR_PASSWORD")
    if password is None:
        password = getpass.getpass(
            f"Password for {args.username} at {args.lighthouse}: "
        )
    if not password:
        print("ERROR: FLEETDOCTOR_PASSWORD is empty.", file=sys.stderr)
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
        curses.wrapper(
            lambda screen: FleetDashboard(
                screen=screen,
                client=client,
                lighthouse_url=args.lighthouse,
                api_version=args.api_version,
                refresh_seconds=args.refresh,
            ).run()
        )
        return 0
    except LighthouseError as exc:
        print(f"ERROR: Lighthouse API request failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            client.logout()
        except LighthouseError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
