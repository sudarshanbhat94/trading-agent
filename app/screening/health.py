"""Describe evidence coverage separately from research or execution eligibility."""
from collections import Counter
from zoneinfo import ZoneInfo

from .financials import valid_income_history


def universe_coverage(screen):
    """Count observed checks, including rejected identities, never assumed coverage."""
    symbols = {r.get('symbol') for r in screen.get('equities', []) if r.get('symbol')}
    excluded = screen.get('exclusions')
    if not isinstance(excluded, dict) or any(not isinstance(k, str) or not k or
            not isinstance(v, str) or not v for k, v in excluded.items()) or symbols & set(excluded):
        return dict(evaluated_count=None, liquid_count=len(symbols), excluded_before_scoring=None,
                    exclusion_reasons=[], coverage_note='Pre-score exclusion coverage is unavailable or inconsistent')
    reasons = Counter(excluded.values())
    return dict(evaluated_count=len(symbols | set(excluded)), liquid_count=len(symbols),
                excluded_before_scoring=len(excluded),
                exclusion_reasons=[dict(reason=r, count=n) for r, n in reasons.most_common(10)],
                coverage_note='Universe checks include rejected history, liquidity and identity checks; only retained stocks receive full scoring')


def discovery_health(screen, rejected, now):
    rows = {r.get('symbol'): r for r in screen.get('equities', []) if r.get('symbol')}
    decision_day = now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
    history_ready = sum(valid_income_history(r.get('fundamentals'), decision_day)
                        for r in rows.values())
    # Rejections can contain several independent predicates. Count each stock
    # once per reason; these counts overlap and must never be added as a funnel.
    by_symbol = {}
    for row in rejected:
        by_symbol.setdefault(row.get('symbol'),set()).update(
            filter(None,(s.strip() for s in row['reason'].split('; '))))
    reasons = Counter()
    for predicates in by_symbol.values():
        for reason in sorted(predicates):
            reasons[reason] += 1
    incomplete = len(rows) - history_ready
    stale = screen.get('status') != 'ok' or screen.get('stale') or screen.get('price_stale')
    return dict(status='stale' if stale else 'incomplete' if incomplete else 'complete',
                **universe_coverage(screen),
                screened=len(rows), earnings_history_ready=history_ready,
                earnings_history_missing=incomplete,
                rejection_counts=[dict(reason=reason, count=count)
                                  for reason, count in sorted(reasons.items(),key=lambda r:(-r[1],r[0]))],
                feed_errors=list(screen.get('feed_errors') or []),
                generated_at=screen.get('generated_at'),
                note='Evidence coverage is separate from quality, entry confirmation and the execution regime. Rejection counts overlap.')
