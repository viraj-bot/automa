"""Standalone Groww API test — no Telegram required.

Uses the sample trade messages from the user to:
  1. Verify API authentication (get_user_profile)
  2. Load instruments CSV
  3. Resolve a sample instrument symbol
  4. Fetch historical candle data for that instrument
  5. Parse the sample messages through the signal parser

Run:  python test_groww_api.py
"""

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# ── Setup ────────────────────────────────────────────────────────────────────

load_dotenv()

# Auto-detect custom CA bundle for corporate proxies
_ca_bundle = Path("data/ca-bundle.pem")
if _ca_bundle.exists() and _ca_bundle.stat().st_size > 0:
    _bundle = str(_ca_bundle.resolve())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
    os.environ.setdefault("SSL_CERT_FILE", _bundle)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_groww")

API_KEY = os.getenv("GROWW_API_TOKEN", "")
API_SECRET = os.getenv("GROWW_API_SECRET", "")
if not API_KEY:
    logger.error("GROWW_API_TOKEN not set in .env — aborting")
    sys.exit(1)
if not API_SECRET or API_SECRET == "your_api_secret_here":
    logger.error("GROWW_API_SECRET not set in .env — aborting")
    sys.exit(1)

# ── Sample messages (from user-provided logs) ────────────────────────────────

SAMPLE_ENTRY = """New option Trade

Buy NIFTY50 17 FEB 25600 PE at ₹135

target 1: 185
target 2: 235

Stoploss: 90

Act Now"""

SAMPLE_EXIT = """Exit Nifty50 17 FEB 25600 PE
Please exit from NIFTY50 17 FEB"""

SAMPLE_BOOK_PROFIT = """Book Profit in INDHOTEL 24 FEB 690 CE at price 17.2"""

# ── Helpers ──────────────────────────────────────────────────────────────────


def divider(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Tests ────────────────────────────────────────────────────────────────────


def test_parser():
    """Parse sample messages — pure local, no API calls."""
    divider("TEST 1: Signal Parser (local)")

    sys.path.insert(0, os.path.dirname(__file__))
    from parser.signal_parser import SignalParser

    parser = SignalParser()

    # Entry
    sig = parser.parse(SAMPLE_ENTRY, message_id=1)
    assert sig is not None, "Entry signal was not parsed"
    print(f"  Entry  → {sig.display_name}")
    print(f"           price={sig.entry_price}  targets={sig.targets}  sl={sig.stoploss}")
    assert sig.targets == [185.0, 235.0], f"Targets wrong: {sig.targets}"
    print("  ✓ Entry parsed correctly (targets fixed!)")

    # Exit
    sig = parser.parse(SAMPLE_EXIT, message_id=2)
    assert sig is not None, "Exit signal was not parsed"
    print(f"  Exit   → {sig.display_name}")
    print("  ✓ Exit parsed correctly")

    # Book Profit
    sig = parser.parse(SAMPLE_BOOK_PROFIT, message_id=3)
    assert sig is not None, "Book profit signal was not parsed"
    print(f"  Book   → {sig.display_name}  exit_price={sig.exit_price}")
    print("  ✓ Book profit parsed correctly")


def test_auth():
    """Exchange API key + secret for access token, then verify with user profile."""
    divider("TEST 2: Groww API Authentication (two-step)")
    from growwapi import GrowwAPI

    # Step 1: Exchange API key + secret for access token
    print("  Step 1: Exchanging API key + secret for access token …")
    try:
        access_token = GrowwAPI.get_access_token(
            api_key=API_KEY,
            secret=API_SECRET,
        )
        print(f"  ✓ Got access token: {str(access_token)[:60]}…")
    except Exception as exc:
        is_ssl = "SSL" in str(exc)
        logger.error("[GROWW ERR] get_access_token failed: %s", exc)
        if is_ssl:
            print(f"  ✗ SSL ERROR: {exc}")
            print("  → Corporate proxy detected. Create data/ca-bundle.pem with your root CA.")
        else:
            print(f"  ✗ Failed to get access token: {exc}")
            print("  → Check GROWW_API_TOKEN and GROWW_API_SECRET in .env")
        return None, False

    # Step 2: Use access token for authenticated requests
    api = GrowwAPI(access_token)
    print("  Step 2: Verifying access token with get_user_profile …")
    try:
        profile = api.get_user_profile()
        print(f"  [GROWW RES] get_user_profile: {profile}")
        print("  ✓ Authenticated successfully!")
        return api, True
    except Exception as exc:
        logger.error("[GROWW ERR] get_user_profile failed: %s", exc)
        print(f"  ✗ Profile fetch failed: {exc}")
        print("  → Access token obtained but profile endpoint failed")
        return api, False


def test_instruments(api):
    """Load instruments CSV and search for a sample instrument."""
    divider("TEST 3: Load Instruments CSV")
    t0 = time.time()
    df = api.get_all_instruments()
    elapsed = time.time() - t0
    print(f"  Loaded {len(df)} instruments in {elapsed:.1f}s")
    print(f"  Columns: {list(df.columns)}")

    # Show a few FNO instruments
    fno = df[df["segment"] == "FNO"] if "segment" in df.columns else df
    print(f"  FNO instruments: {len(fno)}")
    if not fno.empty:
        print(f"  Sample rows:\n{fno.head(3).to_string(index=False)}")

    return df


def test_resolve_symbol(api, df):
    """Try to resolve the sample NIFTY 17 FEB 25600 PE instrument."""
    divider("TEST 4: Resolve Instrument Symbol")

    from datetime import datetime

    underlying = "NIFTY"
    day = 17
    month = "Feb"
    strike = 25600
    otype = "PE"
    yy = datetime.now().year % 100

    groww_sym = f"NSE-{underlying}-{day:02d}{month}{yy}-{int(strike)}-{otype}"
    print(f"  Looking for: {groww_sym}")

    match = df[df["groww_symbol"] == groww_sym]
    if not match.empty:
        row = match.iloc[0]
        print(f"  ✓ FOUND: trading_symbol={row['trading_symbol']}, "
              f"lot_size={row.get('lot_size', 'N/A')}")
        return row["groww_symbol"]
    else:
        print(f"  ✗ Exact match not found for {groww_sym}")
        # Try fuzzy: any NIFTY PE with strike 25600
        if "underlying_symbol" in df.columns:
            mask = (
                (df["underlying_symbol"] == underlying)
                & (df["instrument_type"] == otype)
            )
            fuzzy = df[mask]
            if not fuzzy.empty:
                print(f"  Found {len(fuzzy)} NIFTY PE instruments. Showing first 5:")
                cols = ["groww_symbol", "trading_symbol", "strike_price", "expiry"]
                show_cols = [c for c in cols if c in fuzzy.columns]
                print(fuzzy[show_cols].head(5).to_string(index=False))
                return fuzzy.iloc[0]["groww_symbol"]
        print("  No NIFTY PE instruments found at all")
        return None


def test_historical_candles(api, groww_symbol):
    """Fetch 1-minute candles for the resolved instrument."""
    divider("TEST 5: Historical Candle Data")

    if groww_symbol is None:
        print("  ⏭  Skipped — no instrument resolved")
        return

    from datetime import datetime, timedelta

    # Try to get candles from a recent trading day
    end = datetime.now()
    start = end - timedelta(days=3)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    print(f"  Fetching candles for {groww_symbol}")
    print(f"  Range: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}")

    try:
        data = api.get_historical_candles(
            exchange=api.EXCHANGE_NSE,
            segment=api.SEGMENT_FNO,
            groww_symbol=groww_symbol,
            start_time=start_ms,
            end_time=end_ms,
            candle_interval=api.CANDLE_INTERVAL_MIN_1,
        )
        candles = data.get("candles", [])
        print(f"  [GROWW RES] {len(candles)} candles returned")
        if candles:
            print(f"  First candle: {candles[0]}")
            print(f"  Last candle:  {candles[-1]}")
            print("  ✓ Historical data fetch successful")
        else:
            print("  ⚠ No candles returned (instrument may have expired or market closed)")
            print(f"  Full response: {data}")
    except Exception as exc:
        logger.error("[GROWW ERR] get_historical_candles: %s", exc)
        print(f"  ✗ Failed: {exc}")


def test_ltp(api, groww_symbol):
    """Fetch the last traded price for the instrument."""
    divider("TEST 6: Last Traded Price (LTP)")

    if groww_symbol is None:
        print("  ⏭  Skipped — no instrument resolved")
        return

    try:
        ltp_data = api.get_ltp(
            exchange=api.EXCHANGE_NSE,
            segment=api.SEGMENT_FNO,
            groww_symbol=groww_symbol,
        )
        print(f"  [GROWW RES] get_ltp: {ltp_data}")
        print("  ✓ LTP fetch successful")
    except Exception as exc:
        logger.error("[GROWW ERR] get_ltp: %s", exc)
        print(f"  ✗ Failed: {exc}")


def test_margins(api):
    """Fetch available margin to verify account access."""
    divider("TEST 7: Available Margins")

    try:
        margins = api.get_available_margin_details()
        print(f"  [GROWW RES] get_available_margin_details: {margins}")
        print("  ✓ Margin fetch successful")
    except Exception as exc:
        logger.error("[GROWW ERR] get_available_margin_details: %s", exc)
        print(f"  ✗ Failed: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Automa — Groww API Standalone Test")
    print("=" * 60)

    # Check CA bundle
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        print(f"  Using CA bundle: {os.environ['REQUESTS_CA_BUNDLE']}")

    # Test 1: Parser (no API)
    test_parser()

    # Test 2: Auth
    api, auth_ok = test_auth()
    if api is None:
        print("\n⛔ Cannot proceed — API connection failed.")
        sys.exit(1)

    # Test 3: Instruments (works without user auth)
    df = test_instruments(api)

    # Test 4: Resolve symbol (local lookup, no API)
    groww_symbol = test_resolve_symbol(api, df)

    # Test 5: Historical candles (needs auth)
    test_historical_candles(api, groww_symbol)

    # Test 6: LTP (needs auth)
    test_ltp(api, groww_symbol)

    # Test 7: Margins (needs auth)
    test_margins(api)

    divider("SUMMARY")
    print("  Parser:          ✓ Working (targets fixed)")
    print(f"  SSL/Network:     ✓ Connected to api.groww.in")
    print(f"  Instruments:     ✓ {len(df)} loaded")
    print(f"  Symbol resolve:  {'✓' if groww_symbol else '✗'}")
    if auth_ok:
        print("  Authentication:  ✓ Token valid")
    else:
        print("  Authentication:  ✗ Token expired — regenerate at groww.in/trade-api/api-keys")
    print()


if __name__ == "__main__":
    main()
