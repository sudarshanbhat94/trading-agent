"""Explicitly retired instrument identities; historic ownership stays intact."""
import sqlite3

# Sourced exchange identity, independent of display names and symbol aliases.
# This asset is removed from discovery and all new entries, including manual
# broker orders. Existing owned SELL/protection paths remain available.
RETIRED_INSTRUMENT_KEYS = frozenset({'NSE_EQ|INF204KB14I2'})


def is_retired(key):
    return key in RETIRED_INSTRUMENT_KEYS


def retired_symbols(con):
    try:
        return {r[0] for r in con.execute(
            'SELECT symbol FROM universe WHERE upstox_instrument_key IN (?)',
            tuple(RETIRED_INSTRUMENT_KEYS))}
    except sqlite3.OperationalError:
        # Older research fixtures may have prices without an instrument master.
        return set()
