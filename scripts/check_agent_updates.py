#!/usr/bin/env python3
"""Check installed Office agents and email only those with a newer release.

Runs from cron every Sunday 20:00 Asia/Shanghai. Agents whose installed
version is already the latest are left out of the message. If nobody has
an update, no mail is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Iterable, Mapping

RECIPIENT = "28854583@qq.com"
HEADER = "agent名称，更新日期"
SUBJECT = "已安装 Agent 更新清单"
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_SMTP_ENV = os.path.expanduser("~/.config/agent-update-check/smtp.env")

OFFICECLI_BIN = (
    "/home/ubuntu/.local/lib/node_modules/@officecli/officecli/vendor/officecli"
)
GENOFFICE_BIN = "/usr/bin/genoffice"
UNIVER_PACKAGE_JSON = (
    "/home/ubuntu/.local/lib/univer/node_modules/@univerjs/core/package.json"
)

VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True)
class Release:
    version: tuple[int, ...]
    published_at: datetime


@dataclass(frozen=True)
class Agent:
    name: str
    repo: str
    installed_version: Callable[[], tuple[int, ...]]


def parse_version(text: str) -> tuple[int, ...]:
    match = VERSION_RE.search(text.strip())
    if not match:
        raise ValueError(f"无法解析版本: {text!r}")
    return tuple(int(part) for part in match.group(0).split("."))


def version_is_newer(latest: tuple[int, ...], installed: tuple[int, ...]) -> bool:
    width = max(len(latest), len(installed))
    latest_n = latest + (0,) * (width - len(latest))
    installed_n = installed + (0,) * (width - len(installed))
    return latest_n > installed_n


def format_update_date(published_at: datetime) -> str:
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def build_report(rows: Iterable[tuple[str, str]]) -> str | None:
    lines = [f"{name}，{updated}" for name, updated in rows]
    if not lines:
        return None
    return HEADER + "\n" + "\n".join(lines) + "\n"


def command_version(argv: list[str]) -> tuple[int, ...]:
    completed = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = completed.stdout.strip() or completed.stderr.strip()
    return parse_version(text)


def officecli_version() -> tuple[int, ...]:
    binary = os.environ.get("OFFICECLI_BIN", OFFICECLI_BIN)
    return command_version([binary, "--version"])


def genoffice_version() -> tuple[int, ...]:
    binary = os.environ.get("GENOFFICE_BIN", GENOFFICE_BIN)
    return command_version([binary, "--version"])


def univer_version() -> tuple[int, ...]:
    path = os.environ.get("UNIVER_PACKAGE_JSON", UNIVER_PACKAGE_JSON)
    with open(path, encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    return parse_version(str(version))


AGENTS = (
    Agent("OfficeCLI", "iOfficeAI/OfficeCLI", officecli_version),
    Agent("GenOffice", "genspark-ai/genoffice", genoffice_version),
    Agent("Univer", "dream-num/univer", univer_version),
)


def fetch_latest(repo: str) -> Release:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "agent-update-check",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    published = datetime.fromisoformat(payload["published_at"].replace("Z", "+00:00"))
    return Release(parse_version(payload["tag_name"]), published)


def collect_updates(
    agents: Iterable[Agent],
    fetch: Callable[[str], Release],
) -> tuple[list[tuple[str, str]], list[str]]:
    rows: list[tuple[str, str]] = []
    errors: list[str] = []
    for agent in agents:
        try:
            installed = agent.installed_version()
            latest = fetch(agent.repo)
        except Exception as exc:  # noqa: BLE001 - one agent must not hide the others
            errors.append(f"{agent.name}: {exc}")
            continue
        if version_is_newer(latest.version, installed):
            rows.append((agent.name, format_update_date(latest.published_at)))
    return rows, errors


def load_smtp_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    path = environ.get("AGENT_UPDATE_SMTP_ENV", DEFAULT_SMTP_ENV)
    values: dict[str, str] = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_USE_TLS"):
        if environ.get(key):
            values[key] = environ[key]
    values.setdefault("SMTP_HOST", "smtp.qq.com")
    values.setdefault("SMTP_PORT", "587")
    values.setdefault("SMTP_USER", RECIPIENT)
    values.setdefault("SMTP_FROM", values.get("SMTP_USER", RECIPIENT))
    values.setdefault("SMTP_USE_TLS", "1")
    if not values.get("SMTP_PASSWORD"):
        raise RuntimeError(
            "缺少 SMTP 授权码。QQ 邮箱在网页「设置 → 账号 → SMTP」生成授权码后，"
            f"写入 {path} 的 SMTP_PASSWORD。"
        )
    return values


def deliver(body: str, config: Mapping[str, str]) -> None:
    message = EmailMessage()
    message["Subject"] = SUBJECT
    message["From"] = config.get("SMTP_FROM") or config["SMTP_USER"]
    message["To"] = RECIPIENT
    message.set_content(body, charset="utf-8")

    host = config.get("SMTP_HOST", "smtp.qq.com")
    port = int(config.get("SMTP_PORT", "587"))
    use_tls = config.get("SMTP_USE_TLS", "1") not in {"0", "false", "False"}
    if port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
    try:
        smtp.ehlo()
        if use_tls and port != 465:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        smtp.send_message(message)
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPServerDisconnected:
            pass


def log(text: str) -> None:
    now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} {text}", flush=True)


def in_sunday_window(now: datetime | None = None) -> bool:
    """True during Sunday 20:00–20:59 Asia/Shanghai, the scheduled mail hour."""
    current = datetime.now(SHANGHAI) if now is None else now.astimezone(SHANGHAI)
    return current.weekday() == 6 and current.hour == 20


def run(
    agents: Iterable[Agent] = AGENTS,
    fetch: Callable[[str], Release] = fetch_latest,
    send: Callable[[str], None] | None = None,
    dry_run: bool = False,
    scheduled: bool = False,
    now: datetime | None = None,
) -> int:
    if scheduled and not in_sunday_window(now):
        log("未到周日 20:00（Asia/Shanghai），跳过")
        return 0
    rows, errors = collect_updates(agents, fetch)
    for error in errors:
        log(f"检查失败 {error}")
    report = build_report(rows)
    if report is None:
        log("没有更新，不发送邮件")
        return 1 if errors else 0
    log("发现更新:\n" + report.rstrip("\n"))
    if dry_run:
        sys.stdout.write(report)
        log("dry-run: 不发送邮件")
        return 1 if errors else 0
    if send is None:
        config = load_smtp_config()
        send = lambda body: deliver(body, config)  # noqa: E731
    try:
        send(report)
    except Exception as exc:  # noqa: BLE001
        log(f"邮件发送失败: {exc}")
        return 1
    log(f"已发送更新清单到 {RECIPIENT}")
    return 1 if errors else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查已安装的 Office agent 是否有更新，并在有更新时发邮件。")
    parser.add_argument("--dry-run", action="store_true", help="只打印清单，不发送邮件")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="只在周日 20:00–20:59（Asia/Shanghai）执行",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run(dry_run=args.dry_run, scheduled=args.scheduled)


if __name__ == "__main__":
    raise SystemExit(main())
