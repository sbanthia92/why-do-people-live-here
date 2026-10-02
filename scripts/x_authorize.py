"""Let an X account authorize this project's app, and save its access token to .env.

Use this when the developer app belongs to one account but posts should go out from
another. It runs X's PIN-based OAuth 1.0a flow:

  1. The script prints a link.
  2. Open it in a browser where you are logged in as the account that should post,
     and click Authorize app.
  3. X shows a PIN. Type it back into the script.

The new account's access token and secret are written to X_ACCESS_TOKEN and
X_ACCESS_SECRET in .env. They are never printed.

Needs X_API_KEY and X_API_SECRET (the app's keys) in .env, and the app set to
"Read and write" in its user authentication settings.

Usage:  python scripts/x_authorize.py
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tweepy
from dotenv import load_dotenv

ENV_FILE = ROOT / ".env"


def _save(values: dict[str, str]) -> None:
    """Set KEY=value lines in .env, replacing existing ones and appending missing ones."""
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(lambda _, line=line: line, text)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text)


def main() -> int:
    load_dotenv(ENV_FILE)
    missing = [k for k in ("X_API_KEY", "X_API_SECRET") if not os.getenv(k)]
    if missing:
        print(f"Not set in .env: {', '.join(missing)}. Copy them from the app's Keys and tokens page.")
        return 2

    # "oob" asks X to show a PIN instead of redirecting to a callback URL
    auth = tweepy.OAuth1UserHandler(os.environ["X_API_KEY"], os.environ["X_API_SECRET"], callback="oob")
    try:
        url = auth.get_authorization_url()
    except tweepy.TweepyException as exc:
        print(f"X refused to start authorization: {exc}")
        print("Check X_API_KEY and X_API_SECRET, and that the app has user authentication set up.")
        return 1

    print("1. Open this link in a browser where you are logged in as the account that should post:")
    print(f"\n   {url}\n")
    print("2. Click 'Authorize app'. X will show a PIN.")
    pin = input("3. Type the PIN here: ").strip()

    try:
        token, secret = auth.get_access_token(pin)
    except tweepy.TweepyException as exc:
        print(f"X rejected the PIN: {exc}. Run the script again for a fresh link and PIN.")
        return 1

    _save({"X_ACCESS_TOKEN": token, "X_ACCESS_SECRET": secret})
    print("\nSaved the access token and secret to .env (not shown).")

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=token,
        access_token_secret=secret,
    )
    try:
        me = client.get_me()
        print(f"Posts will go out as @{me.data.username} ({me.data.name}).")
    except tweepy.TweepyException as exc:
        print(f"Saved, but could not confirm the account: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
