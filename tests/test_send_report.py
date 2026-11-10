"""Tests for scripts/send_report.py: emailing the cost report without leaking it."""

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "send_report.py"
_spec = importlib.util.spec_from_file_location("send_report", _SCRIPT)
send_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(send_report)

REPORT = """# Cost report: 2 Nov – 9 Nov 2026

- **Spent this week:** $0.76
- **Posts this week:** 1

## Where the money went this week

| Stage | Cost | Share |
|---|---|---|
| web search | $0.60 | 79% |
| writing | $0.10 | 13% |
"""

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "user",
    "SMTP_PASSWORD": "hunter2",
    "SMTP_FROM": "Reports <reports@example.com>",
    "REPORT_EMAIL_TO": "owner@example.com",
}


class FakeSMTP:
    """Records what would have been sent. `kind` says which constructor was used."""

    instances: ClassVar[list] = []

    def __init__(self, host, port, timeout=None, context=None, kind="plain"):
        self.host, self.port, self.kind, self.calls, self.sent = host, port, kind, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append("send")
        self.sent = message


class FakeSMTPSSL(FakeSMTP):
    def __init__(self, host, port, timeout=None, context=None):
        super().__init__(host, port, timeout, context, kind="ssl")


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(send_report.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(send_report.smtplib, "SMTP_SSL", FakeSMTPSSL)


def test_report_becomes_plain_text_with_the_title_as_subject_and_tables_lined_up():
    subject, body = send_report.to_text(REPORT)

    assert subject == "Cost report: 2 Nov – 9 Nov 2026"
    assert "Spent this week: $0.76" in body and "**" not in body
    assert "Where the money went this week" in body and "## " not in body
    assert "|" not in body and "---" not in body
    # Columns are padded to the widest cell, so they line up in a plain-text email
    assert "Stage       Cost   Share" in body
    assert "web search  $0.60  79%" in body
    assert "writing     $0.10  13%" in body


def test_email_is_sent_with_starttls_before_login_on_the_default_port():
    send_report.send(REPORT, ENV)

    (server,) = FakeSMTP.instances
    assert (server.host, server.port, server.kind) == ("smtp.example.com", 587, "plain")
    assert server.calls == ["starttls", ("login", "user", "hunter2"), "send"]
    assert server.sent["From"] == "Reports <reports@example.com>"
    assert server.sent["To"] == "owner@example.com"
    assert server.sent["Subject"] == "Cost report: 2 Nov – 9 Nov 2026"
    assert "$0.76" in server.sent.get_content()


def test_port_465_uses_tls_from_the_start_and_never_plain_text():
    send_report.send(REPORT, {**ENV, "SMTP_PORT": "465"})

    (server,) = FakeSMTP.instances
    assert (server.port, server.kind) == (465, "ssl")
    assert server.calls == [("login", "user", "hunter2"), "send"]  # no STARTTLS needed


def test_missing_settings_are_named_without_connecting_or_echoing_values():
    env = {k: v for k, v in ENV.items() if k not in ("SMTP_PASSWORD", "REPORT_EMAIL_TO")}

    with pytest.raises(ValueError) as error:
        send_report.send(REPORT, env)

    assert "SMTP_PASSWORD" in str(error.value) and "REPORT_EMAIL_TO" in str(error.value)
    assert "smtp.example.com" not in str(error.value) and "hunter2" not in str(error.value)
    assert FakeSMTP.instances == []


def test_main_reports_success_without_printing_the_report(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["send_report.py", str(report)])

    assert send_report.main() == 0

    output = capsys.readouterr().out
    assert output.strip() == "Report emailed."
    assert "$0.76" not in output


def test_main_names_only_the_kind_of_error_never_the_host_or_password(monkeypatch, tmp_path, capsys):
    class Refusing(FakeSMTP):
        def __init__(self, *args, **kwargs):
            raise ConnectionRefusedError("could not reach smtp.example.com as user with hunter2")

    monkeypatch.setattr(send_report.smtplib, "SMTP", Refusing)
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["send_report.py", str(report)])

    assert send_report.main() == 3

    output = capsys.readouterr().out
    assert "Not sent (ConnectionRefusedError)." in output
    assert "smtp.example.com" not in output and "hunter2" not in output
