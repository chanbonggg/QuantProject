"""
2025-2026 데이터 수집 스크립트 (파일 저장용)

DB 저장 없이 Parquet 파일로 저장.
나중에 DB에 일괄 삽입 가능.

저장 위치:
  data/collected/prices_2025_2026.parquet   — OHLCV 주가
  data/collected/fred_2025_2026.parquet     — FRED 거시지표
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "quant_us"))
from quant_us.utils.logger import logger
from quant_us.data.collectors.price_collector import SP500_TICKERS
from quant_us.data.collectors.fred_collector import FRED_SERIES, _fetch_series

# ── 설정 ────────────────────────────────────────────────────────────────────

START_DATE = "2025-01-01"
END_DATE   = "2026-03-28"   # yfinance end는 exclusive, 하루 더
OUTPUT_DIR = PROJECT_ROOT / "data" / "collected"
PRICE_FILE = OUTPUT_DIR / "prices_2025_2026.parquet"
FRED_FILE  = OUTPUT_DIR / "fred_2025_2026.parquet"

BATCH_SIZE = 50   # yfinance 배치 크기 (한 번에 요청할 티커 수)
BATCH_DELAY = 2   # 배치 간 대기 (초)


# ── 주가 수집 ────────────────────────────────────────────────────────────────

def collect_prices() -> pd.DataFrame:
    """yfinance 배치 다운로드로 전체 SP500 주가 수집."""
    logger.info(f"[주가 수집] 시작: {START_DATE} ~ {END_DATE}, {len(SP500_TICKERS)}개 티커")

    # 추가 티커 (SPY, ^VIX, ^VIX3M 등 벤치마크/지수)
    extra_tickers = ["SPY", "SHY", "TLT", "^VIX", "^VIX3M"]
    all_tickers = SP500_TICKERS + extra_tickers

    all_frames = []
    total_batches = (len(all_tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx, start in enumerate(range(0, len(all_tickers), BATCH_SIZE), start=1):
        batch = all_tickers[start : start + BATCH_SIZE]
        logger.info(f"[주가 수집] 배치 {batch_idx}/{total_batches}: {len(batch)}개 티커")

        try:
            raw = yf.download(
                tickers=batch,
                start=START_DATE,
                end=END_DATE,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
            )

            if raw.empty:
                logger.warning(f"[주가 수집] 배치 {batch_idx}: 빈 데이터")
                continue

            # 단일 티커면 컬럼이 flat, 복수면 MultiIndex
            if len(batch) == 1:
                ticker = batch[0]
                df = raw.copy()
                df.columns = [c.lower() for c in df.columns]
                df["ticker"] = ticker
                df = df.reset_index()
                df = df.rename(columns={"index": "date", "Date": "date"})
                df["date"] = pd.to_datetime(df["date"]).dt.date
                all_frames.append(df)
            else:
                for ticker in batch:
                    try:
                        if ticker not in raw.columns.get_level_values(0):
                            continue
                        df = raw[ticker].copy()
                        df.columns = [c.lower() for c in df.columns]
                        df = df.dropna(how="all")
                        if df.empty:
                            continue
                        df["ticker"] = ticker
                        df = df.reset_index()
                        df = df.rename(columns={"index": "date", "Date": "date"})
                        df["date"] = pd.to_datetime(df["date"]).dt.date
                        all_frames.append(df)
                    except Exception as e:
                        logger.debug(f"[주가 수집] {ticker} 파싱 실패: {e}")

        except Exception as e:
            logger.error(f"[주가 수집] 배치 {batch_idx} 실패: {e}")

        if batch_idx < total_batches:
            time.sleep(BATCH_DELAY)

    if not all_frames:
        logger.error("[주가 수집] 수집된 데이터 없음")
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)

    # 컬럼 정리 (DB 스키마와 동일하게)
    result["adj_close"] = result.get("close", result.get("adj close", None))
    result["source"] = "yfinance"
    result["market_cap"] = None

    keep_cols = ["ticker", "date", "open", "high", "low", "close", "adj_close",
                 "volume", "market_cap", "source"]
    result = result[[c for c in keep_cols if c in result.columns]]

    logger.info(f"[주가 수집] 완료: {len(result):,}행, {result['ticker'].nunique()}개 티커, "
                f"날짜 범위={result['date'].min()} ~ {result['date'].max()}")
    return result


# ── FRED 수집 ────────────────────────────────────────────────────────────────

def collect_fred() -> pd.DataFrame:
    """FRED 시리즈 전체 수집."""
    logger.info(f"[FRED 수집] 시작: {START_DATE} ~ {END_DATE}, {len(FRED_SERIES)}개 시리즈")

    collected_at = datetime.now()
    all_rows = []

    for i, series_id in enumerate(FRED_SERIES, start=1):
        try:
            series = _fetch_series(series_id, START_DATE)
            if series.empty:
                logger.warning(f"[FRED 수집] {series_id}: 빈 데이터")
                continue

            # END_DATE 이전 데이터만
            series = series[series.index <= pd.Timestamp(END_DATE)]

            for ts, val in series.items():
                if pd.notna(val):
                    all_rows.append({
                        "series_id": series_id,
                        "date": ts.date(),
                        "value": float(val),
                        "collected_at": collected_at,
                    })

            logger.info(f"[FRED 수집] ({i}/{len(FRED_SERIES)}) {series_id}: {len(series)}행")
            time.sleep(1.0)  # Rate limit

        except Exception as e:
            logger.error(f"[FRED 수집] {series_id} 실패: {e}")

    if not all_rows:
        logger.error("[FRED 수집] 수집된 데이터 없음")
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)
    logger.info(f"[FRED 수집] 완료: {len(result):,}행, {result['series_id'].nunique()}개 시리즈")
    return result


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("=" * 70)
    logger.info(f"2025-2026 데이터 수집 시작 (파일 저장 모드)")
    logger.info(f"저장 위치: {OUTPUT_DIR}")
    logger.info("=" * 70)

    # 1. 주가 수집
    prices_df = collect_prices()
    if not prices_df.empty:
        prices_df.to_parquet(PRICE_FILE, index=False)
        logger.info(f"[저장] 주가: {PRICE_FILE} ({len(prices_df):,}행)")
    else:
        logger.error("[저장] 주가 데이터 없음 — 파일 미저장")

    # 2. FRED 수집
    fred_df = collect_fred()
    if not fred_df.empty:
        fred_df.to_parquet(FRED_FILE, index=False)
        logger.info(f"[저장] FRED: {FRED_FILE} ({len(fred_df):,}행)")
    else:
        logger.error("[저장] FRED 데이터 없음 — 파일 미저장")

    logger.info("=" * 70)
    logger.info("수집 완료")
    logger.info(f"  주가: {PRICE_FILE.name if not prices_df.empty else '실패'}")
    logger.info(f"  FRED: {FRED_FILE.name if not fred_df.empty else '실패'}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
