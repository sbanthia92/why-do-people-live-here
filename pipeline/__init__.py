"""Pipeline package — sequential stages that take a candidate from selection to publishing."""

from dotenv import load_dotenv

# Load .env before any pipeline module reads its settings at import time
# (the Wikimedia User-Agent is built when candidate_selector is imported).
load_dotenv()
