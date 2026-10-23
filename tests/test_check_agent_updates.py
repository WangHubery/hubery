import email
import socketserver
from email import policy
from email.header import decode_header, make_header
import threading
import unittest
from datetime import datetime, timezone

from scripts.check_agent_updates import (
    HEADER,
    RECIPIENT,
    SUBJECT,
    Agent,
    Release,
    build_report,
    collect_updates,
    deliver,
    in_sunday_window,
    parse_version,
    run,
    version_is_newer,
)


class VersionTests(unittest.TestCase):
    def test_parse_command_and_tag(self) -> None:
        self.assertEqual(parse_version("1.0.158"), (1, 0, 158))
        self.assertEqual(parse_version("genoffice 0.12.29\n"), (0, 12, 29))
        self.assertEqual(parse_version("v1.2.0"), (1, 2, 0))

    def test_newer_compares_each_component(self) -> None:
        self.assertTrue(version_is_newer((0, 12, 29), (0, 12, 9)))
        self.assertTrue(version_is_newer((1, 0, 159), (1, 0, 158)))
        self.assertFalse(version_is_newer((1, 2, 0), (1, 2)))
        self.assertFalse(version_is_newer((1, 0, 158), (1, 0, 158)))


class ReportTests(unittest.TestCase):
    def test_omits_agents_without_an_update(self) -> None:
        agents = (
            Agent("OfficeCLI", "office", lambda: (1, 0, 158)),
            Agent("GenOffice", "gen", lambda: (0, 12, 29)),
            Agent("Univer", "univer", lambda: (1, 2, 0)),
        )
        releases = {
            "office": Release((1, 0, 200), datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc)),
            "gen": Release((0, 12, 29), datetime(2026, 10, 22, 14, 11, tzinfo=timezone.utc)),
            "univer": Release((1, 1, 0), datetime(2026, 10, 1, tzinfo=timezone.utc)),
        }
        rows, errors = collect_updates(agents, lambda repo: releases[repo])
        self.assertEqual(errors, [])
        self.assertEqual(rows, [("OfficeCLI", "2026-10-23")])
        self.assertEqual(build_report(rows), "agent名称，更新日期\nOfficeCLI，2026-10-23\n")

    def test_empty_report_is_not_a_blank_list(self) -> None:
        self.assertIsNone(build_report([]))

    def test_header_is_exact(self) -> None:
        report = build_report([("GenOffice", "2026-10-22"), ("Univer", "2026-10-22")])
        self.assertEqual(
            report,
            f"{HEADER}\nGenOffice，2026-10-22\nUniver，2026-10-22\n",
        )


class ScheduleTests(unittest.TestCase):
    def test_window_is_sunday_20_shanghai(self) -> None:
        sunday_20 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)  # 20:00 CST
        sunday_19 = datetime(2026, 10, 25, 11, 59, tzinfo=timezone.utc)
        monday_20 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(in_sunday_window(sunday_20))
        self.assertFalse(in_sunday_window(sunday_19))
        self.assertFalse(in_sunday_window(monday_20))

    def test_scheduled_run_skips_other_hours_without_sending(self) -> None:
        sent: list[str] = []
        agents = (Agent("OfficeCLI", "office", lambda: (1, 0, 1)),)
        code = run(
            agents,
            lambda _repo: Release((9, 0, 0), datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)),
            send=sent.append,
            scheduled=True,
            now=datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])


class MailTests(unittest.TestCase):
    def test_run_does_not_send_when_nothing_changed(self) -> None:
        sent: list[str] = []
        agents = (Agent("OfficeCLI", "office", lambda: (1, 0, 158)),)
        code = run(
            agents,
            lambda _repo: Release((1, 0, 158), datetime(2026, 10, 20, tzinfo=timezone.utc)),
            send=sent.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_deliver_writes_only_the_update_list(self) -> None:
        captured: list[bytes] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.wfile.write(b"220 local\r\n")
                collecting = False
                body: list[bytes] = []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    if collecting:
                        if line == b".\r\n":
                            captured.append(b"".join(body))
                            self.wfile.write(b"250 ok\r\n")
                            collecting = False
                        else:
                            body.append(line)
                        continue
                    command = line.strip().split(b" ", 1)[0].upper()
                    if command == b"EHLO":
                        self.wfile.write(b"250-local\r\n250 AUTH PLAIN\r\n")
                    elif command == b"AUTH":
                        self.wfile.write(b"235 ok\r\n")
                    elif command == b"DATA":
                        self.wfile.write(b"354 go\r\n")
                        collecting = True
                    elif command == b"QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        break
                    else:
                        self.wfile.write(b"250 ok\r\n")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            report = build_report([("Univer", "2026-10-22")])
            assert report is not None
            deliver(
                report,
                {
                    "SMTP_HOST": "127.0.0.1",
                    "SMTP_PORT": str(port),
                    "SMTP_USER": "28854583@qq.com",
                    "SMTP_PASSWORD": "test-auth-code",
                    "SMTP_FROM": "28854583@qq.com",
                    "SMTP_USE_TLS": "0",
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(len(captured), 1)
        message = email.message_from_bytes(captured[0].replace(b"\r\n", b"\n"), policy=policy.default)
        self.assertEqual(message["To"], RECIPIENT)
        self.assertEqual(str(make_header(decode_header(message["Subject"]))), SUBJECT)
        self.assertEqual(message.get_content().replace("\r\n", "\n"), report)


if __name__ == "__main__":
    unittest.main()
