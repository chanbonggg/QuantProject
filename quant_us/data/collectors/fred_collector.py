"""
FRED 거시지표 수집기

fredapi 라이브러리 우선 사용, FRED_API_KEY 없으면 FRED 웹 API 직접 호출로 폴백.
수집 시리즈 (12개):
  금리: DFF, DGS3MO, DGS2, DGS10, DGS30
  크레딧: BAMLH0A0HYM2, BAMLC0A0CM
  경기: UNRATE, CPIAUCSL, T10Y2Y
  변동성: VIXCLS, VXVCLS
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extensions
import requests
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from db.init import get_pg_connection
from utils.logger import logger

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

FRED_SERIES: list[str] = [
    # 금리 (5개)
    "DFF",         # Federal Funds Effective Rate
    "DGS3MO",      # 3-Month Treasury Bill
    "DGS2",        # 2-Year Treasury
    "DGS10",       # 10-Year Treasury
    "DGS30",       # 30-Year Treasury
    # 크레딧 (2개)
    "BAMLH0A0HYM2",  # High Yield OAS
    "BAMLC0A0CM",    # Investment Grade OAS
    # 경기 (3개)
    "UNRATE",        # Unemployment Rate
    "CPIAUCSL",      # CPI-U (All items)
    "T10Y2Y",        # 10-Year minus 2-Year Treasury Spread
    # 변동성 (2개)
    "VIXCLS",        # VIX
    "VXVCLS",        # VIX3M (CBOE S&P 500 3-Month Volatility Index)
]

FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_DELAY = 0.5  # 요청 간 대기 (초)
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _get_api_key() -> Optional[str]:
    """환경변수에서 FRED API 키 반환. 없으면 None."""
    return os.getenv("FRED_API_KEY")


def _fetch_via_fredapi(series_id: str, start: str) -> pd.Series:
    """fredapi 라이브러리로 시리즈 데이터 가져오기."""
    from fredapi import Fred  # type: ignore

    api_key = _get_api_key()
    fred = Fred(api_key=api_key)
    data = fred.get_series(series_id, observation_start=start)
    return data


def _fetch_via_web_api(series_id: str, start: str, api_key: str) -> pd.Series:
    """FRED 웹 API 직접 호출로 시리즈 데이터 가져오기."""
    params = {
        "series_id": series_id,
        "observation_start": start,
        "api_key": api_key,
        "file_type": "json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(FRED_API_BASE, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()

            observations = payload.get("observations", [])
            records = {
                obs["date"]: float(obs["value"])
                for obs in observations
                if obs["value"] != "."
            }
            series = pd.Series(records)
            series.index = pd.to_datetime(series.index)
            return series

        except requests.HTTPError as e:
            logger.warning(f"FRED 웹 API {series_id} HTTP 에러 (시도 {attempt}/{MAX_RETRIES}): {e}")
        except (KeyError, ValueError) as e:
            logger.warning(f"FRED 웹 API {series_id} 파싱 에러 (시도 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    raise RuntimeError(f"FRED 웹 API {series_id} 수집 최종 실패")


def _fetch_without_key(series_id: str, start: str) -> pd.Series:
    """API 키 없이 FRED 공개 CSV 엔드포인트로 시리즈 데이터 가져오기."""
    csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(csv_url, timeout=30)
            resp.raise_for_status()

            from io import StringIO
            df = pd.read_csv(StringIO(resp.text), parse_dates=["DATE"], index_col="DATE")
            series = df.iloc[:, 0]
            series = pd.to_numeric(series, errors="coerce").dropna()
            series = series[series.index >= pd.Timestamp(start)]
            return series

        except Exception as e:
            logger.warning(f"FRED 공개 API {series_id} 에러 (시도 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    raise RuntimeError(f"FRED 공개 API {series_id} 수집 최종 실패")


def _fetch_series(series_id: str, start: str) -> pd.Series:
    """
    우선순위에 따라 FRED 데이터 가져오기.
    1. FRED_API_KEY 있으면 fredapi 사용
    2. FRED_API_KEY 있으면 웹 API 직접 호출
    3. API 키 없으면 공개 CSV 엔드포인트 폴백
    """
    api_key = _get_api_key()

    # 1. fredapi 라이브러리 시도
    if api_key:
        try:
            import fredapi  # noqa: F401
            logger.debug(f"{series_id}: fredapi 라이브러리로 수집 시도")
            return _fetch_via_fredapi(series_id, start)
        except ImportError:
            logger.debug("fredapi 미설치 — 웹 API로 폴백")
        except Exception as e:
            logger.warning(f"{series_id}: fredapi 실패 ({e}) — 웹 API로 폴백")

    # 2. 웹 API 직접 호출
    if api_key:
        logger.debug(f"{series_id}: FRED 웹 API로 수집 시도")
        return _fetch_via_web_api(series_id, start, api_key)

    # 3. API 키 없음 — 공개 CSV 엔드포인트
    logger.debug(f"{series_id}: API 키 없음 — 공개 CSV 엔드포인트 폴백")
    return _fetch_without_key(series_id, start)


def _get_last_collected_date(conn: psycopg2.extensions.connection, series_id: str) -> Optional[str]:
    """DB에서 해당 시리즈의 마지막 수집 날짜 반환. 없으면 None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(date) FROM raw.fred_series WHERE series_id = %s",
        (series_id,),
    )
    result = cur.fetchone()
    cur.close()

    if result and result[0] is not None:
        return str(result[0])
    return None


def _upsert_series(
    conn: psycopg2.extensions.connection,
    series_id: str,
    series: pd.Series,
) -> int:
    """시리즈 데이터를 raw.fred_series에 upsert. 삽입된 행 수 반환."""
    if series.empty:
        return 0

    collected_at = datetime.now()
    rows = [
        (series_id, ts.date(), float(val), collected_at)
        for ts, val in series.items()
        if pd.notna(val)
    ]

    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO raw.fred_series (series_id, date, value, collected_at)
            VALUES %s
            ON CONFLICT (series_id, date) DO UPDATE SET
                value = EXCLUDED.value,
                collected_at = EXCLUDED.collected_at
            """,
            rows,
            page_size=500,
        )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def collect_all(start: str = "2000-01-01", conn: Optional[psycopg2.extensions.connection] = None) -> int:
    """
    전체 FRED 시리즈 수집 (증분).

    이미 저장된 날짜 이후만 수집한다.
    start는 DB에 데이터가 없을 때의 기본 시작일.

    Args:
        start: 시작 날짜 ('YYYY-MM-DD'), 기본값 '2000-01-01'
        conn: psycopg2 연결 (None이면 자동 생성)

    Returns:
        삽입된 총 행 수
    """
    close_conn = conn is None
    if conn is None:
        conn = get_pg_connection()

    logger.info(f"[FRED 수집] 전체 시리즈 수집 시작 (기본 시작일: {start}, 시리즈 수: {len(FRED_SERIES)})")

    success_count = 0
    fail_count = 0
    total_inserted = 0

    try:
        for series_id in FRED_SERIES:
            # 증분 수집: 마지막 수집일 이후부터
            last_date = _get_last_collected_date(conn, series_id)
            fetch_start = last_date if last_date else start

            logger.debug(f"{series_id}: {fetch_start} 이후 수집 시작")

            try:
                series = _fetch_series(series_id, fetch_start)
                inserted = _upsert_series(conn, series_id, series)
                total_inserted += inserted
                logger.info(f"[FRED 수집] {series_id}: {inserted}건 저장")
                success_count += 1
            except Exception as e:
                logger.error(f"[FRED 수집] {series_id}: 수집 실패 — {e}")
                fail_count += 1

            time.sleep(REQUEST_DELAY)
    finally:
        if close_conn:
            conn.close()

    logger.info(
        f"[FRED 수집] 완료: 성공 {success_count}, 실패 {fail_count} / 전체 {len(FRED_SERIES)}, 총 {total_inserted}행 삽입"
    )
    return total_inserted


def get_series(series_id: str, as_of_date: str, conn: Optional[psycopg2.extensions.connection] = None) -> Optional[float]:
    """
    특정 날짜 이전 최신값 반환.

    as_of_date 당일 포함, 이전 날짜 중 가장 최근 값.
    데이터 없으면 None 반환.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT value
            FROM raw.fred_series
            WHERE series_id = %s
              AND date <= %s::date
            ORDER BY date DESC
            LIMIT 1
            """,
            (series_id, as_of_date),
        )
        result = cur.fetchone()
        cur.close()

        return float(result[0]) if result else None
    finally:
        if close_conn:
            conn.close()


def get_macro_snapshot(date_str: str, conn: Optional[psycopg2.extensions.connection] = None) -> dict[str, Optional[float]]:
    """
    모든 FRED 시리즈의 해당 날짜 스냅샷 반환.

    각 시리즈에 대해 date_str 당일 포함 이전 최신값을 반환.
    반환 형식: {series_id: value_or_None}
    """
    close_conn = conn is None
    if conn is None:
        conn = get_pg_connection()
    try:
        snapshot: dict[str, Optional[float]] = {}

        cur = conn.cursor()
        for series_id in FRED_SERIES:
            cur.execute(
                """
                SELECT value
                FROM raw.fred_series
                WHERE series_id = %s
                  AND date <= %s::date
                ORDER BY date DESC
                LIMIT 1
                """,
                (series_id, date_str),
            )
            result = cur.fetchone()
            snapshot[series_id] = float(result[0]) if result else None
        cur.close()

        logger.debug(f"거시지표 스냅샷 반환 (기준일: {date_str}, {len(snapshot)}개 시리즈)")
        return snapshot
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# 단독 실행
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    collect_all()
