"""Shared runtime for the LoanDesk AI mock ecosystem.

Every mock service is built from the same pieces: a simulated clock shared through
Postgres, seeded RNG streams, a per-service Postgres schema with an append-only event
log, fault injection, and a scheduler that runs on simulated time.
"""

DISCLAIMER = (
    "Simulator for demonstration only. Not affiliated with or endorsed by EquiLend, Wematch, "
    "FIS or Bloomberg; all names are trademarks of their respective owners. All data is synthetic."
)
