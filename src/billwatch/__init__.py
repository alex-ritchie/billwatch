"""billwatch — legislative bill tracker & email digest.

Pipeline: fetch (LegiScan) → filter (keywords/committees) → diff (SQLite) → digest (email).
"""

__version__ = "0.1.0"
