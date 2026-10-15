"""Comparable annual-income history; fiscal dates are not publication dates."""
from datetime import date
import math
import re


def fiscal_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise ValueError('An exact fiscal period-end date is required')
    return date.fromisoformat(value)


def finite_amount(value):
    if isinstance(value,bool):
        raise ValueError('A numeric statement amount is required')
    try:
        number=float(value)
    except (TypeError,ValueError,OverflowError) as exc:
        raise ValueError('A numeric statement amount is required') from exc
    if not math.isfinite(number):
        raise ValueError('A finite statement amount is required')
    return number


def consecutive_periods(periods,limit=4):
    """Keep the latest uninterrupted annual chain, not disconnected old years."""
    for period in periods:fiscal_date(period)
    ordered=sorted(periods,reverse=True)
    if not ordered:return []
    retained=[ordered[0]]
    for previous in ordered[1:]:
        if len(retained)>=limit:break
        if not 330 <= (fiscal_date(retained[-1])-fiscal_date(previous)).days <= 400:break
        retained.append(previous)
    return list(reversed(retained))


def valid_income_history(f,asof=None):
    """Counts alone cannot prove comparable periods or consistent earnings.

    This verifies the normalized history contract, not exchange verification,
    audit opinion, filing publication time, or commercial data permission.
    """
    try:
        rows=f['earnings_periods']
        if not isinstance(rows,list) or not rows:return False
        currency=f['statement_currency']
        if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):return False
        periods=[r['period_end'] for r in rows]
        amounts=[finite_amount(r['annual_income']) for r in rows]
        if asof is not None and not 0 <= (fiscal_date(asof)-fiscal_date(f['period_end'])).days <= 550:
            return False
        if (len(set(periods))!=len(periods) or periods!=sorted(periods)
                or consecutive_periods(periods,len(periods))!=periods
                or any(r['statement_currency']!=currency for r in rows)
                or periods[-1]!=f['period_end'] or amounts[-1]!=finite_amount(f['annual_income'])):
            return False
        return (finite_amount(f['earnings_years'])==len(rows)
                and finite_amount(f['positive_earnings_years'])==sum(v>0 for v in amounts))
    except (KeyError,TypeError,ValueError,OverflowError):
        return False
