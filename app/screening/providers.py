"""Public providers. Official exchange events, secondary statement data.

All captures carry first-observed time. Fiscal period ends are never used as
publication dates. Numerical fundamentals are not inferred from headlines.
"""
import csv
import io
import math
import re
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from .financials import fiscal_date, finite_amount, consecutive_periods

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
    if not isinstance(now,datetime) or now.tzinfo is None:
        raise ValueError('An aware statement decision time is required')
    if not isinstance(payload,dict) or not isinstance(payload.get('timeseries'),dict):
        raise ValueError('Financial statement series unavailable')
    result=payload['timeseries'].get('result')
    if payload['timeseries'].get('error') or not isinstance(result,list):
        raise ValueError('Financial statement provider response invalid')
    series, currencies = {}, {}
    for item in result:
        if not isinstance(item,dict):raise ValueError('Financial statement block invalid')
        for name in TYPES:
            if name not in item:continue
            rows=item[name]
            if not isinstance(rows,list):raise ValueError('Financial statement rows invalid')
            for row in rows:
                try:
                    period=row['asOfDate'];day=fiscal_date(period)
                    if day>now.astimezone(IST).date():continue
                    if row.get('periodType') not in (None,'12M' if name.startswith('annual') else '3M'):
                        raise ValueError('Statement duration does not match its series')
                    value=finite_amount(row['reportedValue']['raw'])
                    currency=row.get('currencyCode')
                    if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):
                        raise ValueError('Statement currency unavailable')
                except (KeyError,TypeError) as exc:
                    raise ValueError('Financial statement point invalid') from exc
                values=series.setdefault(name,{})
                currency_points=currencies.setdefault(name,{})
                if period in values and (values[period]!=value or currency_points[period]!=currency):
                    raise ValueError('Conflicting same-period financial statement values')
                values[period]=value;currency_points[period]=currency
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
    if (now.astimezone(IST).date() - fiscal_date(period)).days > 550:
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
        return finite_amount(100 * (a / b - 1)) if b > 0 else None
    equity, debt, revenue, ocf = (at(n) for n in (
        "annualStockholdersEquity", "annualTotalDebt", "annualTotalRevenue", "annualOperatingCashFlow"))
    retained=consecutive_periods(incomes)
    historical = [incomes[p] for p in retained]
    annual_growth = [growth("annualNetIncome", p) for p in retained[1:]]
    annual_growth = [v for v in annual_growth if v is not None]
    quarterly = {}
    for name in ("quarterlyTotalRevenue", "quarterlyNetIncome"):
        vals = series.get(name, {})
        if vals:
            cur = max(vals)
            prev = [k for k in vals if 330 <= (datetime.fromisoformat(cur) - datetime.fromisoformat(k)).days <= 400]
            if prev and vals[max(prev)] > 0:
                quarterly[name] = dict(period=cur, yoy_pct=finite_amount(100*(vals[cur]/vals[max(prev)]-1)))
    return dict(period_end=period, statement_currency=currency, annual_income=income, annual_revenue=revenue,
        roe_pct=finite_amount(100*(income/equity)) if equity is not None and equity > 0 else None,
        debt_equity=finite_amount(debt/equity) if debt is not None and debt >= 0 and equity is not None and equity > 0 else None,
        operating_cash_flow=ocf, cash_conversion=finite_amount(ocf/income) if ocf is not None and income > 0 else None,
        profit_margin_pct=finite_amount(100*(income/revenue)) if revenue is not None and revenue > 0 else None,
        earnings_growth_pct=growth("annualNetIncome", period),
        revenue_growth_pct=growth("annualTotalRevenue", period),
        positive_earnings_years=sum(v > 0 for v in historical), earnings_years=len(historical),
        earnings_periods=[dict(period_end=p,annual_income=incomes[p],statement_currency=currency) for p in retained],
        excluded_earnings_periods=sorted(set(incomes)-set(retained)),
        history_basis="latest uninterrupted annual periods in one reporting currency",
        financial_contract_version="annual-statements-v2",
        earnings_growth_std_pct=finite_amount(statistics.pstdev(annual_growth)) if len(annual_growth) >= 2 else None,
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


def fetch_chain(http, symbol, now):
    """Use the current expiry-specific request used by NSE's own chain page."""
    response = http.get(NSE+"/api/option-chain-contract-info", params={"symbol":symbol})
    response.raise_for_status()
    today = now.astimezone(IST).date()
    expiries = sorted(datetime.strptime(x, "%d-%b-%Y").date()
                      for x in response.json()["expiryDates"]
                      if datetime.strptime(x, "%d-%b-%Y").date() >= today)
    if not expiries:
        raise ValueError("no unexpired index options")
    expiry = expiries[0].strftime("%d-%b-%Y")
    response = http.get(NSE+"/api/option-chain-v3", params={
        "type":"Indices", "symbol":symbol, "expiry":expiry})
    response.raise_for_status()
    records = response.json()["records"]
    published = exchange_time(records["timestamp"])
    if not 0 <= (now-published).total_seconds() <= 4*86400:
        raise ValueError("option chain too old or future dated")
    rows = [dict(row, expiryDate=expiry) for row in records["data"]
            if (row.get("expiryDates") or row.get("expiryDate")) == expiry]
    if not rows:
        raise ValueError("no rows for selected expiry")
    return records.get("underlyingValue"), rows, published, expiry


def fetch_delivery(http, session):
    url = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_"+session.strftime("%d%m%Y")+".csv"
    response = http.get(url)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    if not reader.fieldnames or not {"SYMBOL", "SERIES", "DATE1", "DELIV_PER"} <= {k.strip() for k in reader.fieldnames}:
        raise ValueError("delivery report columns changed")
    out = {}
    for raw in reader:
        row = {k.strip():str(v or "").strip() for k,v in raw.items() if k}
        if row.get("SERIES") != "EQ": continue
        if datetime.strptime(row["DATE1"], "%d-%b-%Y").date() != session.date():
            raise ValueError("delivery report returned a different session")
        try:
            pct = float(row["DELIV_PER"])
            if math.isfinite(pct) and 0 <= pct <= 100: out[row["SYMBOL"]] = pct
        except ValueError:
            continue
    if not out: raise ValueError("empty delivery report")
    return out


def fetch_sectors(http):
    response = http.get("https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv")
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    if not reader.fieldnames or not {"Symbol", "Industry", "Series"} <= set(reader.fieldnames):
        raise ValueError("sector constituent columns changed")
    out = {row["Symbol"].strip().upper():row["Industry"].strip() for row in reader
           if row.get("Series") == "EQ" and row.get("Symbol") and row.get("Industry")}
    if not 400 <= len(out) <= 600: raise ValueError("incomplete sector constituent file")
    return out


def fetch_flows(http, session):
    response = http.get(NSE+"/api/fiidiiTradeReact")
    response.raise_for_status()
    out = []
    for row in response.json():
        day = datetime.strptime(row["date"], "%d-%b-%Y").date()
        value = float(str(row["netValue"]).replace(",", ""))
        if row.get("category") in ("DII", "FII/FPI") and day == session.date() and math.isfinite(value):
            out.append(dict(session=day.isoformat(), category=row["category"], net_inr_crore=value))
    if {r["category"] for r in out} != {"DII", "FII/FPI"}:
        raise ValueError("current-session institutional flows unavailable")
    return out
