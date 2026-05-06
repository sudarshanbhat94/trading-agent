#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Nubra current-price market watch.")
    parser.add_argument("symbols", nargs="*", default=["NIFTY", "RELIANCE"], help="Symbols to fetch")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--base-url", default=os.getenv("NUBRA_API_BASE_URL", "https://uatapi.nubra.io"))
    parser.add_argument("--token", default=os.getenv("NUBRA_SESSION_TOKEN", ""))
    parser.add_argument("--device-id", default=os.getenv("NUBRA_DEVICE_ID", ""))
    parser.add_argument("--price-scale", type=float, default=float(os.getenv("NUBRA_PRICE_SCALE", "100")))
    args = parser.parse_args()

    if not args.token or not args.device_id:
        raise SystemExit("Set NUBRA_SESSION_TOKEN and NUBRA_DEVICE_ID first.")

    headers = {
        "Accept": "application/json",
        "x-device-id": args.device_id,
        "Authorization": f"Bearer {args.token}",
    }
    for symbol in args.symbols:
        url = f"{args.base_url.rstrip('/')}/optionchains/{urllib.parse.quote(symbol, safe='')}/price"
        query = urllib.parse.urlencode({"exchange": args.exchange})
        request = urllib.request.Request(f"{url}?{query}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"{symbol}: HTTP {exc.code} {body[:500]}")
            continue
        price = _scaled(payload.get("price"), args.price_scale)
        prev_close = _scaled(payload.get("prev_close"), args.price_scale)
        day_pct = ((price - prev_close) / prev_close) * 100 if price and prev_close else None
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "price": price,
                    "prev_close": prev_close,
                    "day_pct": round(day_pct, 3) if day_pct is not None else None,
                    "raw": payload,
                },
                indent=2,
            )
        )


def _scaled(value: object, scale: float) -> float | None:
    try:
        return round(float(value) / scale, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


if __name__ == "__main__":
    main()
