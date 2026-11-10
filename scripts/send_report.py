"""Email a report file over SMTP. Prints nothing about its contents.

Used by the weekly cost workflow: the report holds private numbers and Actions logs are
public, so this prints only whether the email was sent, never the report or the credentials.
Works with any SMTP provider. Settings come from the environment:

  SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, REPORT_EMAIL_TO   (required)
  SMTP_PORT                                                              (default 587)

Port 465 uses TLS from the start; any other port upgrades with STARTTLS. The connection is
never sent unencrypted.

Usage:  python scripts/send_report.py REPORT.md
"""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

_REQUIRED = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "REPORT_EMAIL_TO")


def to_text(markdown: str) -> tuple[str, str]:
    """(subject, plain-text body): the title line, then the report with tables lined up."""
    lines = markdown.strip().splitlines()
    subject = lines[0].removeprefix("# ").strip() if lines else "Report"
    out: list[str] = []
    table: list[list[str]] = []

    def flush() -> None:
        if table:
            widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
            out.extend("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table)
            table.clear()

    for line in lines[1:]:
        text = line.replace("**", "").replace("`", "")
        if text.startswith("|"):
            cells = [c.strip() for c in text.strip().strip("|").split("|")]
            if not all(set(c) <= {"-", ":"} for c in cells):  # skip the |---| separator row
                table.append(cells)
            continue
        flush()
        out.append(text.replace("## ", "", 1) if text.startswith("## ") else text)
    flush()
    return subject, "\n".join(out).strip() + "\n"


def send(markdown: str, env: dict[str, str]) -> None:
    """Email the report. Raises ValueError, naming only what is missing, if not configured."""
    missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise ValueError(f"Missing settings: {', '.join(missing)}")
    subject, body = to_text(markdown)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env["SMTP_FROM"]
    message["To"] = env["REPORT_EMAIL_TO"]
    message.set_content(body)

    host, port = env["SMTP_HOST"], int(env.get("SMTP_PORT") or 587)
    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if port != 465:
            server.starttls(context=context)
        server.login(env["SMTP_USERNAME"], env["SMTP_PASSWORD"])
        server.send_message(message)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        send(Path(sys.argv[1]).read_text(), dict(os.environ))
    except ValueError as exc:
        print(f"Not sent. {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        # Only the kind of error: a connection error can quote the host, and the log is public
        print(f"Not sent ({type(exc).__name__}).")
        return 3
    print("Report emailed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
