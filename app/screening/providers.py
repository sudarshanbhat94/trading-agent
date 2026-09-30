"""Public providers. Official exchange events, secondary statement data.

All captures carry first-observed time. Fiscal period ends are never used as
publication dates. Numerical fundamentals are not inferred from headlines.
"""
import math
import re
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

IST = ZoneInfo("Asia/Kolkata")
NSE = "https://www.nseindia.com"
TYPES = ("annualTotalRevenue", "annualNetIncome", "annualOperatingCashFlow",
         "annualStockholdersEquity", "annualTotalDebt", "annualBasicEPS",
         "quarterlyTotalRevenue", "quarterlyNetIncome")
ADVERSE = re.compile(r"\b(fraud|default|insolvency|forensic audit|rating downgrade|"
                     r"penalty|search and seizure|winding.?up|suspension of trading)\b", re.I)
CATALYST = re.compile(r"\b(order win|award of (?:order|contract)|receipt of (?:order|contract)|"
                      r"credit rating upgrade|acquisition|capacity expansion)\b", re.I)


def exchange_time(value):
    return datetime.strptime(value, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)


def official_url(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host == "nseindia.com" or host.endswith(".nseindia.com")


def events(announcements, meetings, now):
    """Preserve exchange dissemination time, source URL, and uncertainty."""
    out = {}
    for row in announcements:
        try:
            # Dissemination can be later than the company's submission time.
            published = exchange_time(row.get("exchdisstime") or row["an_dt"])
            url = row.get("attchmntFile") or ""
            if not official_url(url) or not 0 <= (now - published).total_seconds() <= 7 * 86400:
                continue
            symbol = str(row["symbol"]).upper()
            title = str(row.get("desc") or "") + ": " + str(row.get("attchmntText") or "")
            classification = "risk_review" if ADVERSE.search(title) else (
                "catalyst_review" if CATALYST.search(title) else "event")
            out.setdefault(symbol, []).append(dict(title=title, url=url,
                published_at=published.isoformat(), classification=classification,
                note="Headline classification; filing requires review"))
        except (KeyError, ValueError, TypeError):
            continue
    calendars = {}
    for row in meetings:
        try:
            if "financial results" not in str(row.get("bm_purpose") or "").lower():
                continue
            published = exchange_time(row["bm_timestamp"])
            when = datetime.strptime(row["bm_date"], "%d-%b-%Y").date()
            if published > now or when < now.astimezone(IST).date():
                continue
            symbol = str(row["bm_symbol"]).upper()
            old = calendars.get(symbol)
            if old is None or published > exchange_time(old["published_raw"]):
                calendars[symbol] = dict(date=when.isoformat(),
                    published_at=published.isoformat(), published_raw=row["bm_timestamp"],
                    url=row.get("attachment") if official_url(row.get("attachment")) else NSE)
        except (KeyError, ValueError, TypeError):
            continue
    return out, calendars


def statements(payload, now):
    """Same-period ratios; cash flow and leverage are not comparable for banks."""
    series, currencies = {}, {}
    for item in (payload.get("timeseries") or {}).get("result") or []:
        for name in TYPES:
            values = {}
            for row in item.get(name) or []:
                try:
                    period = row["asOfDate"]
                    value = float(row["reportedValue"]["raw"])
                    currency = str(row.get("currencyCode") or "")
                    if (math.isfinite(value) and re.fullmatch(r"[A-Z]{3}", currency)
                            and period <= now.astimezone(IST).date().isoformat()):
                        values[period] = value
                        currencies.setdefault(name, {})[period] = currency
                except (KeyError, TypeError, ValueError):
                    continue
            if values:
                series[name] = values
    incomes = series.get("annualNetIncome", {})
    if not incomes:
        raise ValueError("annual earnings with identified currency unavailable")
    period = max(incomes)
    currency = currencies["annualNetIncome"][period]
    if any(vals.get(period) != currency for name, vals in currencies.items()
           if period in vals and name.startswith("annual")):
        raise ValueError("inconsistent statement currencies")
    series = {name:{p:v for p,v in vals.items() if currencies[name][p] == currency}
              for name,vals in series.items()}
    incomes = series["annualNetIncome"]
    if (now.date() - datetime.fromisoformat(period).date()).days > 550:
        raise ValueError("annual statement is too old")
    income = incomes[period]
    def at(name):
        return series.get(name, {}).get(period)
    def growth(name, current):
        earlier = sorted(k for k in series.get(name, {}) if k < current)
        # A growth figure requires comparable consecutive periods, not a gap.
        if not earlier:
            return None
        prev = earlier[-1]
        gap = (datetime.fromisoformat(current) - datetime.fromisoformat(prev)).days
        if not 330 <= gap <= 400:
            return None
        a, b = series[name][current], series[name][prev]
        return 100 * (a / b - 1) if b > 0 else None
    equity, debt, revenue, ocf = (at(n) for n in (
        "annualStockholdersEquity", "annualTotalDebt", "annualTotalRevenue", "annualOperatingCashFlow"))
    historical = [incomes[p] for p in sorted(incomes)[-4:]]
    annual_growth = [growth("annualNetIncome", p) for p in sorted(incomes)]
    annual_growth = [v for v in annual_growth if v is not None]
    quarterly = {}
    for name in ("quarterlyTotalRevenue", "quarterlyNetIncome"):
        vals = series.get(name, {})
        if vals:
            cur = max(vals)
            prev = [k for k in vals if 330 <= (datetime.fromisoformat(cur) - datetime.fromisoformat(k)).days <= 400]
            if prev and vals[max(prev)] > 0:
                quarterly[name] = dict(period=cur, yoy_pct=100*(vals[cur]/vals[max(prev)]-1))
    return dict(period_end=period, statement_currency=currency, annual_income=income, annual_revenue=revenue,
        roe_pct=100*income/equity if equity is not None and equity > 0 else None,
        debt_equity=debt/equity if debt is not None and debt >= 0 and equity is not None and equity > 0 else None,
        operating_cash_flow=ocf, cash_conversion=ocf/income if ocf is not None and income > 0 else None,
        profit_margin_pct=100*income/revenue if revenue is not None and revenue > 0 else None,
        earnings_growth_pct=growth("annualNetIncome", period),
        revenue_growth_pct=growth("annualTotalRevenue", period),
        positive_earnings_years=sum(v > 0 for v in historical), earnings_years=len(historical),
        earnings_growth_std_pct=statistics.pstdev(annual_growth) if len(annual_growth) >= 2 else None,
        roe_basis="annual earnings / ending equity",
        basic_eps=at("annualBasicEPS"), quarterly_growth=quarterly,
        reliability="secondary financial statements; not exchange-verified ratios")


def fetch_statements(http, symbol, now):
    ticker = symbol + ".NS"
    url = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/" + ticker
    response = http.get(url, params=dict(type=",".join(TYPES),
        period1=int((now - timedelta(days=5*366)).timestamp()), period2=int(now.timestamp())))
    response.raise_for_status()
    return statements(response.json(), now)
