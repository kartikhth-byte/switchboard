"""Test-session settings.

The daily USD cap (SHR-018) sums today's live spend in the database. Scripted-model tests record
small costs, so after enough runs on one machine the cap trips and unrelated tests fail with
budget_exceeded. Tests that check the cap set their own; everything else runs uncapped.
"""

import os

os.environ.setdefault("DAILY_USD_CAP", "1000000")
