"""Roster and platform analysis for Democratic U.S. House general-election candidates.

Pipeline: build a roster of Democrats on the November ballot, find their
campaign websites, and classify each one's healthcare position on a tiered
Medicare for All scale with auditable evidence.

See README.md for the two structural problems this is built around: states
that never produce a party nominee, and the ways keyword-matching
"Medicare for All" overcounts support.
"""

__version__ = "0.1.0"
