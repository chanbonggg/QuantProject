"""
주가 수집기
- Polygon.io 우선 → yfinance 폴백
- S&P500 구성종목 OHLCV, 지수, 변동성 지수 수집
- 서바이버십 편향 방지: sp500_changes 이력 저장
- 증분 수집: 이미 수집된 날짜 스킵
- 실패 시 최대 3회 재시도, 지수 백오프
"""

import os
import time
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import psycopg2
import psycopg2.extensions
from psycopg2.extras import execute_values
import requests
from bs4 import BeautifulSoup

from db.init import get_pg_connection
from utils.logger import logger

# ── 상수 ───────────────────────────────────────────────────────────────

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io/v2"

# ── S&P500 전체 503개 티커 (2026-03-21 기준, GitHub 소스, 멀티스레딩)
# 멀티스레딩으로 GIL 이슈 우회 → 병렬 수집
SP500_TICKERS = ["MMM", "AOS", "ABT", "ABBV", "ACN", "ADBE", "AMD", "AES", "AFL", "A", "APD", "ABNB", "AKAM", "ALB", "ARE", "ALGN", "ALLE", "LNT", "ALL", "GOOGL", "GOOG", "MO", "AMZN", "AMCR", "AEE", "AEP", "AXP", "AIG", "AMT", "AWK", "AMP", "AME", "AMGN", "APH", "ADI", "AON", "APA", "APO", "AAPL", "AMAT", "APP", "APTV", "ACGL", "ADM", "ARES", "ANET", "AJG", "AIZ", "T", "ATO", "ADSK", "ADP", "AZO", "AVB", "AVY", "AXON", "BKR", "BALL", "BAC", "BAX", "BDX", "BRK.B", "BBY", "TECH", "BIIB", "BLK", "BX", "XYZ", "BK", "BA", "BKNG", "BSX", "BMY", "AVGO", "BR", "BRO", "BF.B", "BLDR", "BG", "BXP", "CHRW", "CDNS", "CPT", "CPB", "COF", "CAH", "CCL", "CARR", "CVNA", "CAT", "CBOE", "CBRE", "CDW", "COR", "CNC", "CNP", "CF", "CRL", "SCHW", "CHTR", "CVX", "CMG", "CB", "CHD", "CIEN", "CI", "CINF", "CTAS", "CSCO", "C", "CFG", "CLX", "CME", "CMS", "KO", "CTSH", "COIN", "CL", "CMCSA", "FIX", "CAG", "COP", "ED", "STZ", "CEG", "COO", "CPRT", "GLW", "CPAY", "CTVA", "CSGP", "COST", "CTRA", "CRH", "CRWD", "CCI", "CSX", "CMI", "CVS", "DHR", "DRI", "DDOG", "DVA", "DECK", "DE", "DELL", "DAL", "DVN", "DXCM", "FANG", "DLR", "DG", "DLTR", "D", "DPZ", "DASH", "DOV", "DOW", "DHI", "DTE", "DUK", "DD", "ETN", "EBAY", "ECL", "EIX", "EW", "EA", "ELV", "EME", "EMR", "ETR", "EOG", "EPAM", "EQT", "EFX", "EQIX", "EQR", "ERIE", "ESS", "EL", "EG", "EVRG", "ES", "EXC", "EXE", "EXPE", "EXPD", "EXR", "XOM", "FFIV", "FDS", "FICO", "FAST", "FRT", "FDX", "FIS", "FITB", "FSLR", "FE", "FISV", "F", "FTNT", "FTV", "FOXA", "FOX", "BEN", "FCX", "GRMN", "IT", "GE", "GEHC", "GEV", "GEN", "GNRC", "GD", "GIS", "GM", "GPC", "GILD", "GPN", "GL", "GDDY", "GS", "HAL", "HIG", "HAS", "HCA", "DOC", "HSIC", "HSY", "HPE", "HLT", "HOLX", "HD", "HON", "HRL", "HST", "HWM", "HPQ", "HUBB", "HUM", "HBAN", "HII", "IBM", "IEX", "IDXX", "ITW", "INCY", "IR", "PODD", "INTC", "IBKR", "ICE", "IFF", "IP", "INTU", "ISRG", "IVZ", "INVH", "IQV", "IRM", "JBHT", "JBL", "JKHY", "J", "JNJ", "JCI", "JPM", "KVUE", "KDP", "KEY", "KEYS", "KMB", "KIM", "KMI", "KKR", "KLAC", "KHC", "KR", "LHX", "LH", "LRCX", "LW", "LVS", "LDOS", "LEN", "LII", "LLY", "LIN", "LYV", "LMT", "L", "LOW", "LULU", "LYB", "MTB", "MPC", "MAR", "MRSH", "MLM", "MAS", "MA", "MTCH", "MKC", "MCD", "MCK", "MDT", "MRK", "META", "MET", "MTD", "MGM", "MCHP", "MU", "MSFT", "MAA", "MRNA", "MOH", "TAP", "MDLZ", "MPWR", "MNST", "MCO", "MS", "MOS", "MSI", "MSCI", "NDAQ", "NTAP", "NFLX", "NEM", "NWSA", "NWS", "NEE", "NKE", "NI", "NDSN", "NSC", "NTRS", "NOC", "NCLH", "NRG", "NUE", "NVDA", "NVR", "NXPI", "ORLY", "OXY", "ODFL", "OMC", "ON", "OKE", "ORCL", "OTIS", "PCAR", "PKG", "PLTR", "PANW", "PSKY", "PH", "PAYX", "PAYC", "PYPL", "PNR", "PEP", "PFE", "PCG", "PM", "PSX", "PNW", "PNC", "POOL", "PPG", "PPL", "PFG", "PG", "PGR", "PLD", "PRU", "PEG", "PTC", "PSA", "PHM", "PWR", "QCOM", "DGX", "Q", "RL", "RJF", "RTX", "O", "REG", "REGN", "RF", "RSG", "RMD", "RVTY", "HOOD", "ROK", "ROL", "ROP", "ROST", "RCL", "SPGI", "CRM", "SNDK", "SBAC", "SLB", "STX", "SRE", "NOW", "SHW", "SPG", "SWKS", "SJM", "SW", "SNA", "SOLV", "SO", "LUV", "SWK", "SBUX", "STT", "STLD", "STE", "SYK", "SMCI", "SYF", "SNPS", "SYY", "TMUS", "TROW", "TTWO", "TPR", "TRGP", "TGT", "TEL", "TDY", "TER", "TSLA", "TXN", "TPL", "TXT", "TMO", "TJX", "TKO", "TTD", "TSCO", "TT", "TDG", "TRV", "TRMB", "TFC", "TYL", "TSN", "USB", "UBER", "UDR", "ULTA", "UNP", "UAL", "UPS", "URI", "UNH", "UHS", "VLO", "VTR", "VLTO", "VRSN", "VRSK", "VZ", "VRTX", "VTRS", "VICI", "V", "VST", "VMC", "WRB", "GWW", "WAB", "WMT", "DIS", "WBD", "WM", "WAT", "WEC", "WFC", "WELL", "WST", "WDC", "WY", "WSM", "WMB", "WTW", "WDAY", "WYNN", "XEL", "XYL", "YUM", "ZBRA", "ZBH", "ZTS"]  # Total: 503 tickers from GitHub S&P500 constituents

# Rate Limit 제어: 요청 간 delay
REQUEST_DELAY = 0.1  # 초 (멀티스레딩으로 분산)
MAX_WORKERS = 10  # ThreadPoolExecutor 워커 수 (GIL 우회용)
RETRY_MAX = 2
RETRY_DELAYS = [2, 4]  # 지수 백오프 (초)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


# ── 유틸리티 ────────────────────────────────────────────────────────────

def _use_polygon() -> bool:
    """Polygon API 비활성화 (yfinance only)."""
    return False  # 2026-03-21: Polygon Rate Limit 이슈로 yfinance만 사용


def _retry(func, *args, **kwargs):
    """최대 3회 재시도, 지수 백오프."""
    last_error: Optional[Exception] = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            logger.warning(f"시도 {attempt}/{RETRY_MAX} 실패: {e}")
            if attempt < RETRY_MAX:
                time.sleep(delay)
    raise RuntimeError(f"최대 재시도 횟수 초과") from last_error


def _to_date_str(d) -> str:
    """date/datetime/str → 'YYYY-MM-DD' 문자열."""
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    return str(d)


# ── S&P500 구성종목 수집 ────────────────────────────────────────────────

def _fetch_sp500_wikipedia() -> List[str]:
    """Wikipedia에서 S&P500 현재 구성종목 목록 수집."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; QuantUS/1.0)"}
    resp = requests.get(SP500_WIKI_URL, timeout=15, headers=headers)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table", {"id": "constituents"})
    if table is None:
        raise ValueError("Wikipedia S&P500 테이블을 찾을 수 없음")

    tickers: List[str] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if cells:
            ticker = cells[0].get_text(strip=True).replace(".", "-")
            tickers.append(ticker)

    logger.info(f"Wikipedia S&P500 구성종목 {len(tickers)}개 수집 완료")
    return tickers


def _fetch_sp500_changes_wikipedia() -> pd.DataFrame:
    """Wikipedia에서 S&P500 편입/편출 이력 수집."""
    headers = {"User-Agent": "Mozilla/5.0 (quant-research-bot/1.0)"}
    resp = requests.get(SP500_WIKI_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 두 번째 테이블이 변경 이력
    tables = soup.find_all("table", {"class": "wikitable"})
    if len(tables) < 2:
        logger.warning("S&P500 변경 이력 테이블 없음 — 빈 DataFrame 반환")
        return pd.DataFrame(columns=["date", "ticker", "action", "reason", "replacement"])

    rows = []
    for row in tables[1].find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        try:
            change_date = pd.to_datetime(cells[0].get_text(strip=True)).date()
            added = cells[1].get_text(strip=True).replace(".", "-")
            removed = cells[2].get_text(strip=True).replace(".", "-")
            reason = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            if added:
                rows.append({
                    "date": change_date,
                    "ticker": added,
                    "action": "add",
                    "reason": reason,
                    "replacement": removed or None,
                })
            if removed:
                rows.append({
                    "date": change_date,
                    "ticker": removed,
                    "action": "remove",
                    "reason": reason,
                    "replacement": added or None,
                })
        except Exception as e:
            logger.debug(f"S&P500 변경 이력 행 파싱 실패: {e}")

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["date", "ticker", "action", "reason", "replacement"]
    )
    logger.info(f"S&P500 변경 이력 {len(df)}개 수집 완료")
    return df


def save_sp500_changes(conn: psycopg2.extensions.connection, df: pd.DataFrame) -> None:
    """sp500_changes 테이블에 신규 이력만 upsert."""
    if df.empty:
        return

    cur = conn.cursor()
    try:
        for _, row in df.iterrows():
            cur.execute(
                "SELECT 1 FROM raw.sp500_changes WHERE date=%s AND ticker=%s AND action=%s",
                (row["date"], row["ticker"], row["action"]),
            )
            existing = cur.fetchone()
            if existing:
                continue
            cur.execute(
                "INSERT INTO raw.sp500_changes (date, ticker, action, reason, replacement) VALUES (%s,%s,%s,%s,%s)",
                (row["date"], row["ticker"], row["action"], row.get("reason"), row.get("replacement")),
            )
        conn.commit()
    finally:
        cur.close()


# ── Polygon.io 수집 ─────────────────────────────────────────────────────

def _polygon_fetch_ohlcv(ticker: str, from_date: str, to_date: str) -> pd.DataFrame:
    """Polygon.io REST API로 OHLCV 수집."""
    clean_ticker = ticker.replace("^", "")
    url = (
        f"{POLYGON_BASE_URL}/aggs/ticker/{clean_ticker}/range/1/day"
        f"/{from_date}/{to_date}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.rename(columns={
        "t": "timestamp", "o": "open", "h": "high",
        "l": "low", "c": "close", "v": "volume", "vw": "vwap",
    })
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date
    df["adj_close"] = df["close"]  # Polygon adjusted=true이면 close가 이미 조정가
    df["ticker"] = ticker
    df["source"] = "polygon"
    df["market_cap"] = None

    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "market_cap", "source"]
    return df[cols]


# ── yfinance 수집 ──────────────────────────────────────────────────────

def _yfinance_fetch_ohlcv(ticker: str, from_date: str, to_date: str) -> pd.DataFrame:
    """yfinance로 OHLCV 수집."""
    import yfinance as yf

    yf_ticker = yf.Ticker(ticker)
    hist = yf_ticker.history(start=from_date, end=to_date, auto_adjust=True)

    if hist.empty:
        return pd.DataFrame()

    hist = hist.reset_index()
    hist.columns = [c.lower() for c in hist.columns]

    # yfinance date 컬럼은 Timestamp → date
    if "date" in hist.columns:
        hist["date"] = pd.to_datetime(hist["date"]).dt.date
    elif "datetime" in hist.columns:
        hist = hist.rename(columns={"datetime": "date"})
        hist["date"] = pd.to_datetime(hist["date"]).dt.date

    hist["ticker"] = ticker
    hist["adj_close"] = hist["close"]  # auto_adjust=True이므로 close가 조정가
    hist["source"] = "yfinance"
    hist["market_cap"] = None

    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "market_cap", "source"]
    available = [c for c in cols if c in hist.columns]
    return hist[available]


# ── 단일 티커 수집 ─────────────────────────────────────────────────────

def _fetch_ticker_ohlcv(ticker: str, from_date: str, to_date: str) -> pd.DataFrame:
    """yfinance로 단일 티커 OHLCV 수집 (Polygon 비활성화)."""
    # Rate Limit 제어: 요청 전 delay
    time.sleep(REQUEST_DELAY)
    logger.debug(f"{ticker}: yfinance 수집 중... (delay={REQUEST_DELAY}s)")
    return _yfinance_fetch_ohlcv(ticker, from_date, to_date)


# ── 증분 수집 헬퍼 ─────────────────────────────────────────────────────

def _get_last_collected_date(conn: psycopg2.extensions.connection, ticker: str) -> Optional[date]:
    """raw.prices에서 해당 티커의 마지막 수집 날짜 조회."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM raw.prices WHERE ticker = %s", (ticker,))
    result = cur.fetchone()
    cur.close()
    if result and result[0]:
        return result[0] if isinstance(result[0], date) else result[0].date()
    return None


def _upsert_prices(conn: psycopg2.extensions.connection, df: pd.DataFrame) -> int:
    """raw.prices 테이블에 데이터 삽입 (중복 스킵) — 단건 삽입용."""
    if df.empty:
        return 0

    inserted = 0
    cur = conn.cursor()
    try:
        for _, row in df.iterrows():
            try:
                cur.execute(
                    """
                    INSERT INTO raw.prices
                        (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, date) DO NOTHING
                    """,
                    (
                        row["ticker"], row["date"],
                        row.get("open"), row.get("high"), row.get("low"),
                        row.get("close"), row.get("adj_close"),
                        int(row["volume"]) if pd.notna(row.get("volume")) else None,
                        row.get("market_cap"),
                        row.get("source", "unknown"),
                    ),
                )
                inserted += 1
            except Exception as e:
                logger.debug(f"upsert 스킵 ({row.get('ticker')}, {row.get('date')}): {e}")
                conn.rollback()
        conn.commit()
    finally:
        cur.close()

    return inserted


def _bulk_upsert_prices(conn: psycopg2.extensions.connection, df: pd.DataFrame) -> int:
    """raw.prices 테이블에 DataFrame 전체를 한 번에 bulk 삽입 (중복 스킵).

    psycopg2 execute_values 활용 — row-by-row 대비 수십 배 빠름.
    """
    if df.empty:
        return 0

    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "market_cap", "source"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].copy()

    # volume: int 변환
    df["volume"] = df["volume"].apply(lambda v: int(v) if pd.notna(v) else None)

    rows = [tuple(row) for row in df.itertuples(index=False, name=None)]

    with conn.cursor() as cur:
        # 삽입 전 행 수 조회
        cur.execute("SELECT COUNT(*) FROM raw.prices")
        before = cur.fetchone()[0]

        execute_values(
            cur,
            "INSERT INTO raw.prices (ticker,date,open,high,low,close,adj_close,volume,market_cap,source) VALUES %s ON CONFLICT (ticker, date) DO NOTHING",
            rows,
            page_size=500,
        )

        cur.execute("SELECT COUNT(*) FROM raw.prices")
        after = cur.fetchone()[0]

    conn.commit()
    return after - before  # 실제 삽입된 행 수 (중복 제외)


# ── 퍼블릭 인터페이스 ──────────────────────────────────────────────────

def collect_daily(target_date: str, conn: Optional[psycopg2.extensions.connection] = None) -> bool:
    """
    단일 날짜의 모든 대상 티커 주가 수집.

    Args:
        target_date: 수집 날짜 ('YYYY-MM-DD')
        conn: psycopg2 연결 (None이면 자동 생성)

    Returns:
        수집 성공 여부
    """
    try:
        # 1. 수집 대상 티커 구성 (yfinance only, stock만, 지수 제외)
        all_tickers = SP500_TICKERS
        logger.info(f"[수집 시작] {target_date}: {len(all_tickers)}개 티커 (멀티스레딩, {MAX_WORKERS}개 워커)")

        total_inserted = 0
        failed: List[str] = []

        # 2. 멀티스레딩으로 병렬 수집 — 각 워커가 독립 연결 생성
        def _fetch_and_insert(ticker: str) -> tuple:
            """개별 티커 수집 및 DB 삽입"""
            worker_conn = get_pg_connection()
            try:
                last_date = _get_last_collected_date(worker_conn, ticker)
                if last_date and _to_date_str(last_date) >= target_date:
                    return (ticker, 0, None)

                # yfinance history의 end는 exclusive → +1일
                next_day = _to_date_str(datetime.strptime(target_date, "%Y-%m-%d").date() + timedelta(days=1))
                df = _retry(_fetch_ticker_ohlcv, ticker, target_date, next_day)
                n = _upsert_prices(worker_conn, df)
                return (ticker, n, None)
            except Exception as e:
                logger.error(f"{ticker}: 수집 실패 — {e}")
                return (ticker, 0, str(e))
            finally:
                worker_conn.close()

        # ThreadPoolExecutor로 병렬 처리
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_and_insert, ticker): ticker for ticker in all_tickers}

            for i, future in enumerate(as_completed(futures), 1):
                ticker, n, error = future.result()
                total_inserted += n
                if error:
                    failed.append(ticker)

                if i % 50 == 0:
                    logger.debug(f"[진행] {i}/{len(all_tickers)} 완료, 삽입: {total_inserted}행")

        success_count = len(all_tickers) - len(failed)
        logger.info(
            f"[수집 완료] {target_date}: {success_count}/{len(all_tickers)}개 티커 성공, "
            f"{total_inserted}행 삽입, 실패 {len(failed)}개"
            + (f" (실패: {failed[:5]}{'...' if len(failed) > 5 else ''})" if failed else "")
        )
        return len(failed) == 0

    except Exception as e:
        logger.error(f"collect_daily({target_date}) 오류: {e}")
        return False


def collect_range(
    start: str,
    end: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> None:
    """
    날짜 범위 수집 (증분: 이미 수집된 날짜 자동 스킵).

    Args:
        start: 시작 날짜 ('YYYY-MM-DD')
        end: 종료 날짜 ('YYYY-MM-DD')
        conn: psycopg2 연결 (None이면 자동 생성)
    """
    _conn = conn or get_pg_connection()
    close_conn = conn is None

    try:
        # S&P500 구성종목 이력 동기화
        _sync_sp500_changes(_conn)

        # 날짜 범위 내 영업일 목록 생성
        date_range = pd.bdate_range(start=start, end=end)
        logger.info(f"범위 수집: {start} ~ {end}, 영업일 {len(date_range)}일")

        # yfinance only, stock만 수집
        all_tickers = SP500_TICKERS
        logger.info(f"[범위 수집] {start} ~ {end}, {len(all_tickers)}개 티커, 요청 간 delay={REQUEST_DELAY}s")

        # 티커별 bulk 수집 (날짜 범위 한 번에)
        inserted_total = 0
        for i, ticker in enumerate(all_tickers, 1):
            last_date = _get_last_collected_date(_conn, ticker)
            fetch_from = start

            if last_date:
                next_day = last_date + timedelta(days=1)
                fetch_from = _to_date_str(next_day)
                if fetch_from > end:
                    logger.debug(f"{ticker}: 이미 최신 — 스킵")
                    continue

            try:
                # yfinance history의 end는 exclusive → +1일
                fetch_to = _to_date_str(datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1))
                df = _retry(_fetch_ticker_ohlcv, ticker, fetch_from, fetch_to)
                n = _upsert_prices(_conn, df)
                inserted_total += n
                if n > 0:
                    logger.debug(f"[{i}/{len(all_tickers)}] {ticker}: {n}행 삽입 ({fetch_from} ~ {end})")
                else:
                    logger.debug(f"[{i}/{len(all_tickers)}] {ticker}: 새 데이터 없음 ({fetch_from} ~ {end})")
            except Exception as e:
                logger.error(f"[{i}/{len(all_tickers)}] {ticker}: 수집 실패 ({fetch_from}~{end}) — {e}")

        logger.info(f"[범위 수집 완료] {start} ~ {end}: 총 {inserted_total}행 삽입")

    finally:
        if close_conn:
            _conn.close()


def _get_cache_dir(start: str, end: str) -> Path:
    """백필 캐시 디렉토리 경로 반환."""
    project_root = Path(__file__).parent.parent.parent
    return project_root / "data" / "backfill_cache" / f"{start}_{end}"


def backfill_fetch(
    start: str,
    end: str,
    workers: int = MAX_WORKERS,
) -> int:
    """
    DB 연결 없이 yfinance에서 병렬 fetch → 티커별 parquet 저장만 수행.

    DB를 열지 않으므로 여러 연도를 동시에 실행 가능.
    이미 parquet가 있는 티커는 스킵 (중단 후 재시작 지원).

    캐시 경로: data/backfill_cache/{start}_{end}/{TICKER}.parquet

    Returns:
        저장 성공한 티커 수
    """
    cache_dir = _get_cache_dir(start, end)
    cache_dir.mkdir(parents=True, exist_ok=True)
    failed_log = cache_dir / "_failed.txt"

    fetch_to = _to_date_str(
        datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1)
    )
    expected_days = len(pd.bdate_range(start, end))
    expected_min = int(expected_days * 0.6)

    cached_tickers = {p.stem for p in cache_dir.glob("*.parquet")}
    tickers_to_fetch = [t for t in SP500_TICKERS if t not in cached_tickers]

    logger.info(
        f"[백필 fetch] {start} ~ {end}, workers={workers}, "
        f"캐시 스킵: {len(cached_tickers)}개, 대상: {len(tickers_to_fetch)}개"
    )

    if not tickers_to_fetch:
        logger.info(f"[백필 fetch] {start} ~ {end}: 이미 전부 캐시됨")
        return len(cached_tickers)

    failed: list[str] = []
    completed = 0

    def _fetch_and_save(ticker: str) -> bool:
        try:
            df = _retry(_fetch_ticker_ohlcv, ticker, start, fetch_to)
            if df.empty:
                return False
            if len(df) < expected_min:
                logger.warning(
                    f"[백필 fetch] {ticker}: 행 수 부족 ({len(df)}/{expected_min}) — 저장은 진행"
                )
            (cache_dir / f"{ticker}.parquet").write_bytes(
                df.to_parquet(index=False)
            )
            return True
        except Exception as e:
            logger.error(f"[백필 fetch] {ticker}: 실패 — {e}")
            return False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_ticker = {
            executor.submit(_fetch_and_save, t): t for t in tickers_to_fetch
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            if not future.result():
                failed.append(ticker)
            completed += 1
            if completed % 100 == 0:
                logger.info(
                    f"[백필 fetch] {completed}/{len(tickers_to_fetch)} 완료, "
                    f"실패: {len(failed)}개"
                )

    if failed:
        with open(failed_log, "w") as f:
            f.write("\n".join(failed))
        logger.warning(
            f"[백필 fetch] {start} ~ {end}: 실패 {len(failed)}개 → {failed_log}"
        )

    success = len(tickers_to_fetch) - len(failed)
    logger.info(f"[백필 fetch 완료] {start} ~ {end}: 성공 {success}개, 실패 {len(failed)}개")
    return success


def backfill_insert(
    start: str,
    end: str,
    conn: Optional[psycopg2.extensions.connection] = None,
    keep_cache: bool = False,
) -> int:
    """
    backfill_fetch로 저장된 parquet를 읽어 DB에 bulk insert.

    여러 연도의 fetch가 완료된 후 순차적으로 호출해야 함 (PostgreSQL 단일 쓰기).
    DB에 이미 충분한 데이터(거래일 80% 이상)가 있는 티커는 parquet를 읽지 않고 스킵.

    Returns:
        실제 삽입된 행 수 (중복 제외)
    """
    cache_dir = _get_cache_dir(start, end)
    parquet_files = list(cache_dir.glob("*.parquet"))

    if not parquet_files:
        logger.warning(f"[백필 insert] {start} ~ {end}: parquet 없음 — fetch 먼저 실행")
        return 0

    _conn = conn or get_pg_connection()
    close_conn = conn is None

    try:
        expected_days = len(pd.bdate_range(start, end))
        sufficient_threshold = int(expected_days * 0.8)

        # DB에 이미 충분한 티커는 parquet 로드 스킵
        cur = _conn.cursor()
        cur.execute(
            """
            SELECT ticker FROM raw.prices
            WHERE date >= %s AND date <= %s
            GROUP BY ticker HAVING COUNT(*) >= %s
            """,
            (start, end, sufficient_threshold),
        )
        db_sufficient = {row[0] for row in cur.fetchall()}
        cur.close()

        files_to_insert = [
            p for p in parquet_files if p.stem not in db_sufficient
        ]

        logger.info(
            f"[백필 insert] {start} ~ {end}: "
            f"전체 parquet {len(parquet_files)}개, "
            f"DB 충분 스킵 {len(db_sufficient)}개, "
            f"삽입 대상 {len(files_to_insert)}개"
        )

        if not files_to_insert:
            logger.info(f"[백필 insert] {start} ~ {end}: 모두 DB에 존재, 스킵")
            return 0

        dfs = [pd.read_parquet(p) for p in files_to_insert]
        combined = pd.concat(dfs, ignore_index=True)
        logger.info(f"[백필 insert] {len(combined):,}행 bulk insert 시작")

        inserted = _bulk_upsert_prices(_conn, combined)
        logger.info(f"[백필 insert 완료] {start} ~ {end}: {inserted:,}행 삽입")

        # 캐시 정리: parquet만 삭제, _failed.txt 유지
        if not keep_cache:
            for p in cache_dir.glob("*.parquet"):
                p.unlink()
            if not any(cache_dir.iterdir()):
                cache_dir.rmdir()
            logger.debug(f"[백필 insert] parquet 삭제 완료")

        return inserted

    finally:
        if close_conn:
            _conn.close()


def backfill_range(
    start: str,
    end: str,
    conn: Optional[psycopg2.extensions.connection] = None,
    workers: int = MAX_WORKERS,
    keep_cache: bool = False,
) -> int:
    """
    과거 데이터 백필 wrapper — backfill_fetch → backfill_insert 순서로 실행.

    연도별 병렬 수집이 필요하면:
      1. 각 연도에 backfill_fetch() 병렬 실행 (DB 연결 불필요)
      2. 모든 fetch 완료 후 backfill_insert() 순차 실행 (DB 직렬 쓰기)

    Args:
        start: 시작 날짜 ('YYYY-MM-DD')
        end: 종료 날짜 ('YYYY-MM-DD')
        conn: psycopg2 연결 (None이면 자동 생성)
        workers: 병렬 fetch 워커 수 (기본 MAX_WORKERS=10)
        keep_cache: True이면 insert 후 parquet 캐시 유지 (기본 False=삭제)

    Returns:
        총 삽입된 행 수
    """
    backfill_fetch(start, end, workers=workers)
    return backfill_insert(start, end, conn=conn, keep_cache=keep_cache)


def get_sp500_universe(
    target_date: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> List[str]:
    """
    특정 날짜 기준 S&P500 구성종목 반환.

    sp500_changes 이력이 있으면 이력 기반으로 역산.
    없으면 Wikipedia 현재 구성종목 사용.

    Args:
        target_date: 기준 날짜 ('YYYY-MM-DD')
        conn: psycopg2 연결

    Returns:
        티커 리스트
    """
    _conn = conn or get_pg_connection()
    close_conn = conn is None

    try:
        # 1. sp500_changes 이력이 있는지 확인
        cur = _conn.cursor()
        cur.execute("SELECT COUNT(*) FROM raw.sp500_changes")
        count = cur.fetchone()[0]
        cur.close()

        if count == 0:
            # 이력 없음 → Wikipedia 현재 구성종목 반환
            return _retry(_fetch_sp500_wikipedia)

        # 2. 현재 구성종목에서 시작 (Wikipedia)
        current = set(_retry(_fetch_sp500_wikipedia))

        # 3. target_date 이후의 변경 이력을 역으로 적용
        cur = _conn.cursor()
        cur.execute(
            """
            SELECT ticker, action FROM raw.sp500_changes
            WHERE date > %s
            ORDER BY date DESC
            """,
            (target_date,),
        )
        changes = cur.fetchall()
        cur.close()

        for ticker, action in changes:
            if action == "add":
                # target_date 이후에 추가됐으면 → 당시엔 없었음
                current.discard(ticker)
            elif action == "remove":
                # target_date 이후에 제거됐으면 → 당시엔 있었음
                current.add(ticker)

        result = sorted(current)
        logger.info(f"S&P500 유니버스 조회 ({target_date}): {len(result)}개 종목")
        return result

    finally:
        if close_conn:
            _conn.close()


# ── 내부 동기화 헬퍼 ───────────────────────────────────────────────────

def _sync_sp500_changes(conn: psycopg2.extensions.connection) -> None:
    """Wikipedia에서 S&P500 변경 이력을 가져와 DB에 저장."""
    try:
        df = _retry(_fetch_sp500_changes_wikipedia)
        save_sp500_changes(conn, df)
        logger.info("S&P500 변경 이력 동기화 완료")
    except Exception as e:
        logger.warning(f"S&P500 변경 이력 동기화 실패: {e}")


# ── CLI 진입점 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="주가 수집기")
    parser.add_argument("--date", type=str, help="단일 날짜 수집 (YYYY-MM-DD)")
    parser.add_argument("--start", type=str, help="범위 수집 시작 날짜")
    parser.add_argument("--end", type=str, help="범위 수집 종료 날짜")
    parser.add_argument("--universe", type=str, help="S&P500 유니버스 조회 날짜")
    args = parser.parse_args()

    if args.date:
        success = collect_daily(args.date)
        sys.exit(0 if success else 1)
    elif args.start and args.end:
        collect_range(args.start, args.end)
    elif args.universe:
        tickers = get_sp500_universe(args.universe)
        print(f"{args.universe} 기준 S&P500 구성종목 ({len(tickers)}개):")
        print(tickers[:20], "...")
    else:
        parser.print_help()
