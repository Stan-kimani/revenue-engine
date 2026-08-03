"""Shared pytest setup.

Loads .env the same way the application does (scripts/migrate.py and,
eventually, core/config.py all call load_dotenv()), so tests see the same
environment as a normal run without requiring manual shell exports.
"""

from dotenv import load_dotenv

load_dotenv()
