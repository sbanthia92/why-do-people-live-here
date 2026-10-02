"""Check the X credentials in .env without posting anything.

Asks X who the credentials belong to and prints the account. This proves the four keys
are valid and belong to the account you expect. It cannot prove the app is allowed to
post: that is only shown by the first real post.

Costs one user-read request on X's pay-per-use pricing.

Usage:  python scripts/x_check.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tweepy
from dotenv import load_dotenv

X_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


def main() -> int:
    load_dotenv(ROOT / ".env")
    missing = [key for key in X_KEYS if not os.getenv(key)]
    if missing:
        print(f"Not set in .env: {', '.join(missing)}")
        return 2

    from pipeline.publisher import _x_client

    try:
        me = _x_client().get_me()
    except tweepy.Unauthorized:
        print("X rejected the credentials (401). Check that all four keys were copied in full,")
        print("and that the access token was generated after the app was set to Read and Write.")
        return 1
    except tweepy.Forbidden as exc:
        print(f"X refused the request (403): {exc}")
        print("The keys are valid but the app may lack access, or the account has no credits.")
        return 1
    except tweepy.TooManyRequests:
        print("X rate-limited the request (429). Wait a few minutes and try again.")
        return 1
    except tweepy.TweepyException as exc:
        print(f"Could not reach X: {exc!r}")
        return 1

    print(f"Credentials are valid. Posts would go out as @{me.data.username} ({me.data.name}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
