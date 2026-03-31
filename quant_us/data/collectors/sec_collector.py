"""
SEC EDGAR 재무데이터 수집기

- XBRL API: https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json
- ticker → CIK 매핑: https://www.sec.gov/files/company_tickers.json
- 룩어헤드 방지: filed_date(SEC 제출일) 기준으로만 재무 데이터 적용
- Rate Limit: requests 간 0.12초 대기, User-Agent 헤더 필수
"""

import sys
import time
from pathlib import Path
from typing import Optional

import duckdb
import psycopg2
import psycopg2.extensions
import requests
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from db.init import get_pg_connection
from utils.logger import logger

# ── 상수 ──────────────────────────────────────────────────────────────────

BASE_XBRL_URL = "https://data.sec.gov/api/xbrl/companyfacts"
TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
REQUEST_DELAY = 0.12  # SEC Rate Limit 준수

USER_AGENT = "QuantUS Research Bot (contact@example.com)"

# XBRL 태그 → DB 컬럼 매핑 (우선순위 순서: 먼저 발견된 값 사용)
XBRL_TAGS: dict[str, str] = {
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "SalesRevenueNet": "revenue",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "Assets": "total_assets",
    "StockholdersEquity": "stockholders_equity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "stockholders_equity",
    "Liabilities": "total_liabilities",
    "LiabilitiesCurrent": "total_liabilities",
    "NetCashProvidedByUsedInOperatingActivities": "operating_cashflow",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations": "operating_cashflow",
    "CostOfGoodsAndServicesSold": "cost_of_goods_sold",
    "CostOfRevenue": "cost_of_goods_sold",
}

FILING_TYPES = {"10-K", "10-Q"}

# ── 내부 유틸리티 ──────────────────────────────────────────────────────────

def _get_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def _fetch_json(url: str, retries: int = 3) -> Optional[dict]:
    """
    JSON 응답을 가져온다. 3회 재시도 + 지수 백오프.
    """
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY)
            resp = requests.get(url, headers=_get_headers(), timeout=30)

            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                logger.warning(f"Rate limit 429 — {wait}초 대기 후 재시도 ({url})")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                logger.warning(f"404 Not Found: {url}")
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning(f"요청 실패 (시도 {attempt + 1}/{retries}): {e} — {wait}초 대기")
            if attempt < retries - 1:
                time.sleep(wait)

    logger.error(f"최대 재시도 초과: {url}")
    return None


def _load_ticker_cik_map() -> dict[str, str]:
    """
    SEC의 ticker → CIK 매핑 테이블을 로드한다.
    CIK는 10자리 0-패딩 문자열로 반환.
    """
    data = _fetch_json(TICKER_CIK_URL)
    if not data:
        return {}

    mapping: dict[str, str] = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if ticker and cik:
            mapping[ticker] = cik

    logger.info(f"[SEC CIK] 매핑 로드: {len(mapping)}개 종목")
    return mapping


# ── 데이터 파싱 ────────────────────────────────────────────────────────────

def _parse_xbrl_units(units_data: dict) -> list[dict]:
    """
    XBRL companyfacts의 units 데이터에서 연간/분기 filings만 추출한다.
    반환: [{form, accn, filed, end, val}, ...]
    """
    records = []

    # USD, shares, USD/shares 단위 처리
    for unit_type, entries in units_data.items():
        for entry in entries:
            form = entry.get("form", "")
            if form not in FILING_TYPES:
                continue

            filed = entry.get("filed")
            end = entry.get("end")
            val = entry.get("val")

            if not filed or not end or val is None:
                continue

            records.append({
                "form": form,
                "accn": entry.get("accn", ""),
                "filed": filed,
                "end": end,
                "val": val,
            })

    return records


def _build_filing_map(
    facts: dict,
    start_year: int,
) -> dict[tuple[str, str, str], dict]:
    """
    (accn, form, end) 기준으로 각 태그의 최신 값을 모아 filing 딕셔너리를 만든다.
    반환: {(accn, form, end): {col: val, "filed": ..., "form": ..., "end": ...}}
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    filing_map: dict[tuple[str, str, str], dict] = {}

    for xbrl_tag, col_name in XBRL_TAGS.items():
        tag_data = us_gaap.get(xbrl_tag, {})
        units = tag_data.get("units", {})
        if not units:
            continue

        records = _parse_xbrl_units(units)

        for rec in records:
            filed_year = int(rec["filed"][:4])
            if filed_year < start_year:
                continue

            key = (rec["accn"], rec["form"], rec["end"])
            if key not in filing_map:
                filing_map[key] = {
                    "form": rec["form"],
                    "filed": rec["filed"],
                    "end": rec["end"],
                    "accn": rec["accn"],
                }

            # 같은 filing 내에서 태그 값 저장 (이미 값이 있으면 건너뜀 — 첫 태그 우선)
            if col_name not in filing_map[key]:
                filing_map[key][col_name] = rec["val"]

    return filing_map


def _deduplicate_filings(filing_map: dict) -> list[dict]:
    """
    같은 (form, end) 조합 중 가장 최신 filed_date를 가진 레코드만 남긴다.
    수정 제출(amendment)을 올바르게 처리하기 위함.
    """
    # (form, end) → 가장 최신 filing
    best: dict[tuple[str, str], dict] = {}

    for filing in filing_map.values():
        key = (filing["form"], filing["end"])
        existing = best.get(key)

        if existing is None or filing["filed"] > existing["filed"]:
            best[key] = filing

    return list(best.values())


# ── 데이터베이스 저장 ──────────────────────────────────────────────────────

def _upsert_filings(
    ticker: str,
    cik: str,
    filings: list[dict],
) -> int:
    """
    filings를 raw.sec_financials에 upsert 한다.
    이미 존재하는 (ticker, filing_type, period_of_report, filed_date)는 덮어쓴다.
    반환: 저장된 레코드 수
    """
    if not filings:
        return 0

    saved = 0
    pg_conn = get_pg_connection()
    cur = pg_conn.cursor()
    try:
        for filing in filings:
            period = filing["end"]
            filed = filing["filed"]
            form = filing["form"]

            # 기존 레코드 삭제 후 재삽입
            cur.execute(
                """
                DELETE FROM raw.sec_financials
                WHERE ticker = %s AND filing_type = %s AND period_of_report = %s AND filed_date = %s
                """,
                (ticker, form, period, filed),
            )

            cur.execute(
                """
                INSERT INTO raw.sec_financials (
                    ticker, cik, filing_type, period_of_report, filed_date,
                    revenue, net_income, eps_diluted, total_assets,
                    stockholders_equity, total_liabilities, operating_cashflow,
                    cost_of_goods_sold, collected_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    ticker,
                    cik,
                    form,
                    period,
                    filed,
                    filing.get("revenue"),
                    filing.get("net_income"),
                    filing.get("eps_diluted"),
                    filing.get("total_assets"),
                    filing.get("stockholders_equity"),
                    filing.get("total_liabilities"),
                    filing.get("operating_cashflow"),
                    filing.get("cost_of_goods_sold"),
                ),
            )
            saved += 1
        pg_conn.commit()
        logger.info(f"[SEC 저장] {ticker}: {saved}개 filing 저장 완료")
    except Exception as e:
        pg_conn.rollback()
        logger.error(f"[SEC 저장] {ticker} 저장 실패: {e}")
        raise
    finally:
        cur.close()
        pg_conn.close()

    return saved


# ── 공개 인터페이스 ────────────────────────────────────────────────────────

def _get_last_filed_date(ticker: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """DB에 저장된 ticker의 가장 최신 filed_date를 반환한다."""
    row = conn.execute(
        "SELECT MAX(filed_date) FROM raw.sec_financials WHERE ticker = ?",
        [ticker],
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def collect_financials(
    ticker: str,
    start_year: int = 2010,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> int:
    """
    지정된 ticker의 SEC 재무 데이터를 수집해 raw.sec_financials에 저장한다.

    Args:
        ticker: 주식 티커 (예: 'AAPL')
        start_year: 수집 시작 연도 (filed_date 기준)
        conn: psycopg2 커넥션 (None이면 새로 생성 후 자동 종료)

    Returns:
        삽입된 행 수
    """
    close_conn = conn is None
    if conn is None:
        from db.init import get_connection
        conn = get_connection()

    ticker = ticker.upper()
    logger.info(f"[SEC 수집] {ticker} 시작: start_year={start_year}")

    try:
        # 1. ticker → CIK 변환
        cik_map = _load_ticker_cik_map()
        cik = cik_map.get(ticker)

        if not cik:
            logger.error(f"[SEC 수집] {ticker}: CIK 없음")
            return 0

        logger.info(f"[SEC 수집] {ticker} CIK={cik}")

        # 2. 증분 수집: DB의 마지막 filed_date 확인 (DuckDB로 읽기)
        last_filed = _get_last_filed_date(ticker, conn)
        if last_filed:
            logger.info(f"[SEC 수집] {ticker}: 마지막 저장 filed_date={last_filed}")

        # 3. XBRL companyfacts 다운로드
        url = f"{BASE_XBRL_URL}/CIK{cik}.json"
        facts = _fetch_json(url)

        if not facts:
            logger.error(f"[SEC 수집] {ticker}: XBRL 다운로드 실패 (CIK={cik})")
            return 0

        # 4. 파싱
        filing_map = _build_filing_map(facts, start_year)
        filings = _deduplicate_filings(filing_map)

        if not filings:
            logger.warning(f"[SEC 수집] {ticker}: 수집된 filing 없음")
            return 0

        # 5. 증분 필터: 이미 저장된 filed_date 이하 제외
        if last_filed:
            new_filings = [f for f in filings if f["filed"] > last_filed]
            skipped = len(filings) - len(new_filings)
            if skipped:
                logger.info(f"[SEC 수집] {ticker}: {skipped}개 filing 스킵 (이미 저장됨)")
            filings = new_filings

        if not filings:
            logger.info(f"[SEC 수집] {ticker}: 새로운 filing 없음")
            return 0

        logger.info(f"[SEC 수집] {ticker}: {len(filings)}개 신규 filing 파싱")

        # 6. DB 저장 (PostgreSQL로 쓰기 — _upsert_filings 내부에서 pg_conn 생성)
        saved = _upsert_filings(ticker, cik, filings)
        logger.info(f"[SEC 수집] {ticker} 완료: {saved}개 행 삽입")
        return saved

    except Exception as e:
        logger.error(f"[SEC 수집] {ticker} 오류: {e}")
        return 0
    finally:
        if close_conn and conn:
            conn.close()


def get_latest_financials(
    ticker: str,
    as_of_date: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[dict]:
    """
    as_of_date 기준 filed_date 이전의 가장 최신 재무 데이터를 반환한다.
    룩어헤드 방지: filed_date <= as_of_date 조건 적용.

    Args:
        ticker: 주식 티커
        as_of_date: 기준일 (YYYY-MM-DD)
        conn: DuckDB 커넥션 (None이면 get_connection()으로 생성)

    Returns:
        최신 재무 데이터 dict 또는 None
    """
    close_conn = conn is None
    if conn is None:
        from db.init import get_connection
        conn = get_connection()

    ticker = ticker.upper()

    try:
        result = conn.execute(
            """
            SELECT
                ticker, cik, filing_type, period_of_report, filed_date,
                revenue, net_income, eps_diluted, total_assets,
                stockholders_equity, total_liabilities, operating_cashflow,
                cost_of_goods_sold, collected_at
            FROM raw.sec_financials
            WHERE ticker = ?
              AND filed_date <= CAST(? AS DATE)
            ORDER BY filed_date DESC, period_of_report DESC
            LIMIT 1
            """,
            [ticker, as_of_date],
        ).fetchone()

        if not result:
            logger.warning(f"재무 데이터 없음: {ticker} (as_of={as_of_date})")
            return None

        columns = [
            "ticker", "cik", "filing_type", "period_of_report", "filed_date",
            "revenue", "net_income", "eps_diluted", "total_assets",
            "stockholders_equity", "total_liabilities", "operating_cashflow",
            "cost_of_goods_sold", "collected_at",
        ]
        return dict(zip(columns, result))

    except Exception as e:
        logger.error(f"재무 데이터 조회 실패 ({ticker}): {e}")
        return None
    finally:
        if close_conn and conn:
            conn.close()


def get_financials_as_of(
    ticker: str,
    as_of_date: str,
    filing_type: Optional[str] = None,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> list[dict]:
    """
    as_of_date 기준 filed_date 이전의 재무 데이터 목록을 반환한다.

    Args:
        ticker: 주식 티커
        as_of_date: 기준일 (YYYY-MM-DD)
        filing_type: '10-K' 또는 '10-Q' (None이면 전체)
        conn: DuckDB 커넥션 (None이면 get_connection()으로 생성)

    Returns:
        재무 데이터 list[dict]
    """
    close_conn = conn is None
    if conn is None:
        from db.init import get_connection
        conn = get_connection()

    ticker = ticker.upper()

    try:
        query = """
            SELECT
                ticker, cik, filing_type, period_of_report, filed_date,
                revenue, net_income, eps_diluted, total_assets,
                stockholders_equity, total_liabilities, operating_cashflow,
                cost_of_goods_sold, collected_at
            FROM raw.sec_financials
            WHERE ticker = ?
              AND filed_date <= CAST(? AS DATE)
        """
        params: list = [ticker, as_of_date]

        if filing_type:
            query += " AND filing_type = ?"
            params.append(filing_type)

        query += " ORDER BY filed_date DESC, period_of_report DESC"

        rows = conn.execute(query, params).fetchall()

        columns = [
            "ticker", "cik", "filing_type", "period_of_report", "filed_date",
            "revenue", "net_income", "eps_diluted", "total_assets",
            "stockholders_equity", "total_liabilities", "operating_cashflow",
            "cost_of_goods_sold", "collected_at",
        ]
        return [dict(zip(columns, row)) for row in rows]

    except Exception as e:
        logger.error(f"재무 데이터 조회 실패 ({ticker}): {e}")
        return []
    finally:
        if close_conn and conn:
            conn.close()


def collect_batch(
    tickers: list[str],
    start_year: int = 2010,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> dict[str, str]:
    """
    여러 ticker를 순차적으로 수집한다.

    Args:
        tickers: 티커 목록
        start_year: 수집 시작 연도
        conn: psycopg2 커넥션 (None이면 새로 생성 후 자동 종료)

    Returns:
        {ticker: 'success' | 'failed' | 'no_cik'} 결과 맵
    """
    close_conn = conn is None
    if conn is None:
        conn = get_pg_connection()

    results: dict[str, str] = {}
    total = len(tickers)

    # ticker-CIK 맵을 한 번만 로드
    cik_map = _load_ticker_cik_map()

    try:
        for i, ticker in enumerate(tickers, 1):
            ticker = ticker.upper()
            logger.info(f"[{i}/{total}] {ticker} 수집 중...")

            if ticker not in cik_map:
                logger.warning(f"CIK 없음: {ticker}")
                results[ticker] = "no_cik"
                continue

            try:
                _collect_single_with_cik(ticker, cik_map[ticker], start_year, conn)
                results[ticker] = "success"
            except Exception as e:
                logger.error(f"{ticker} 수집 실패: {e}")
                results[ticker] = "failed"

    finally:
        if close_conn:
            conn.close()

    success_count = sum(1 for v in results.values() if v == "success")
    logger.info(f"배치 수집 완료: {success_count}/{total} 성공")
    return results


def _collect_single_with_cik(
    ticker: str,
    cik: str,
    start_year: int,
    conn: psycopg2.extensions.connection,
) -> None:
    """
    CIK가 이미 알려진 경우 XBRL 다운로드부터 시작한다.
    collect_batch에서 CIK 맵을 재사용할 때 사용.
    """
    url = f"{BASE_XBRL_URL}/CIK{cik}.json"
    facts = _fetch_json(url)

    if not facts:
        raise RuntimeError(f"XBRL 다운로드 실패: {ticker} (CIK={cik})")

    filing_map = _build_filing_map(facts, start_year)
    filings = _deduplicate_filings(filing_map)

    if not filings:
        logger.warning(f"수집된 filing 없음: {ticker}")
        return

    # 증분 필터: 이미 저장된 filed_date 이하 제외
    last_filed = _get_last_filed_date(ticker, conn)
    if last_filed:
        new_filings = [f for f in filings if f["filed"] > last_filed]
        skipped = len(filings) - len(new_filings)
        if skipped:
            logger.info(f"{ticker} — {skipped}개 filing 스킵 (이미 저장됨)")
        filings = new_filings

    if not filings:
        logger.info(f"{ticker} — 새로운 filing 없음, 수집 생략")
        return

    try:
        saved = _upsert_filings(conn, ticker, cik, filings)
        conn.commit()
        logger.info(f"{ticker} — {saved}개 레코드 저장")
    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"DB 저장 실패: {e}") from e


# ── CLI 진입점 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SEC EDGAR 재무데이터 수집기")
    parser.add_argument("ticker", help="수집할 ticker (예: AAPL)")
    parser.add_argument(
        "--start-year", type=int, default=2010, help="수집 시작 연도 (기본: 2010)"
    )
    parser.add_argument(
        "--as-of", help="최신 재무 조회 기준일 (YYYY-MM-DD)"
    )
    args = parser.parse_args()

    if args.as_of:
        data = get_latest_financials(args.ticker, args.as_of)
        if data:
            print(f"\n{args.ticker} 최신 재무 ({args.as_of} 기준):")
            for k, v in data.items():
                print(f"  {k}: {v}")
        else:
            print(f"{args.ticker}: 데이터 없음")
    else:
        collect_financials(args.ticker, args.start_year)
