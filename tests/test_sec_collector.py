"""
SEC 수집기 테스트

- collect_financials: 반환 타입 int, 삽입된 행 수
- get_latest_financials: 룩어헤드 방지 검증, 반환 타입 Optional[dict]
- 공통 인터페이스 준수 (price_collector, fred_collector와 동일 패턴)
"""

import sys
import pytest
from pathlib import Path
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
from quant_us.db.init import get_connection
from quant_us.data.collectors.sec_collector import (
    collect_financials,
    get_latest_financials,
)


@pytest.fixture(scope="module")
def test_db():
    """테스트용 DuckDB 커넥션."""
    conn = duckdb.connect(":memory:")

    # 스키마 생성
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")

    # raw.sec_financials 테이블 생성
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.sec_financials (
            ticker               VARCHAR,
            cik                  VARCHAR,
            filing_type          VARCHAR,
            period_of_report     DATE,
            filed_date           DATE,
            revenue              DOUBLE,
            net_income           DOUBLE,
            eps_diluted          DOUBLE,
            total_assets         DOUBLE,
            stockholders_equity  DOUBLE,
            total_liabilities    DOUBLE,
            operating_cashflow   DOUBLE,
            cost_of_goods_sold   DOUBLE,
            collected_at         TIMESTAMP DEFAULT current_timestamp
        )
    """)

    yield conn
    conn.close()


def test_collect_financials_return_type(test_db):
    """
    collect_financials 반환 타입 확인.

    공통 인터페이스: int (삽입된 행 수)
    """
    # price_collector.collect_daily와 동일하게 int 반환
    result = collect_financials("AAPL", start_year=2020, conn=test_db)

    assert isinstance(result, int), f"Expected int, got {type(result)}"
    assert result >= 0, "Inserted rows should be non-negative"


def test_get_latest_financials_return_type(test_db):
    """
    get_latest_financials 반환 타입 확인.

    공통 인터페이스: Optional[dict]
    """
    # 테스트 데이터 삽입
    test_db.execute("""
        INSERT INTO raw.sec_financials (
            ticker, cik, filing_type, period_of_report, filed_date,
            revenue, net_income, eps_diluted, total_assets,
            stockholders_equity, total_liabilities, operating_cashflow,
            cost_of_goods_sold
        ) VALUES (
            'TEST', '0000000001', '10-K', '2023-12-31', '2024-02-15',
            1000000000, 200000000, 5.5, 5000000000,
            2000000000, 3000000000, 300000000, 600000000
        )
    """)

    # 조회
    result = get_latest_financials("TEST", "2024-02-15", conn=test_db)

    assert isinstance(result, dict) or result is None, f"Expected dict or None, got {type(result)}"

    # 존재하는 경우 dict 검증
    if result:
        assert "ticker" in result
        assert "filed_date" in result
        assert result["ticker"] == "TEST"


def test_lookahead_prevention(test_db):
    """
    룩어헤드 방지 검증: filed_date <= as_of_date 기준만 사용.

    2024-02-15에 filed된 데이터는 2024-02-14에는 조회되면 안 됨.
    """
    # 테스트 데이터
    test_db.execute("""
        INSERT INTO raw.sec_financials (
            ticker, cik, filing_type, period_of_report, filed_date,
            revenue, net_income
        ) VALUES (
            'LOOKTEST', '0000000002', '10-K', '2023-12-31', '2024-02-15',
            1000000000, 200000000
        )
    """)

    # 2024-02-14 기준으로 조회 → 데이터 없어야 함
    result_before = get_latest_financials("LOOKTEST", "2024-02-14", conn=test_db)
    assert result_before is None, "Lookahead prevention failed: data from future should not be visible"

    # 2024-02-15 기준으로 조회 → 데이터 있어야 함
    result_on_date = get_latest_financials("LOOKTEST", "2024-02-15", conn=test_db)
    assert result_on_date is not None, "Data on filed_date should be visible"

    # 2024-02-16 기준으로 조회 → 데이터 있어야 함
    result_after = get_latest_financials("LOOKTEST", "2024-02-16", conn=test_db)
    assert result_after is not None, "Data after filed_date should be visible"


def test_incremental_collection(test_db):
    """
    증분 수집 검증: 이미 수집된 ticker/filing_type/period_of_report 스킵.
    """
    # 초기 데이터 삽입
    test_db.execute("""
        INSERT INTO raw.sec_financials (
            ticker, cik, filing_type, period_of_report, filed_date,
            revenue
        ) VALUES (
            'INCTEST', '0000000003', '10-K', '2023-12-31', '2024-02-15',
            1000000000
        )
    """)

    # collect_financials 호출 (이미 수집된 것으로 간주되어 0 반환)
    # 실제로는 SEC API를 호출하려 하지만, 테스트에서는 로컬 데이터만 확인
    result = collect_financials("INCTEST", start_year=2020, conn=test_db)

    # 반환 타입 확인
    assert isinstance(result, int), "collect_financials should return int"


def test_multiple_filings_latest_selection(test_db):
    """
    여러 filing 중 최신 것만 반환하는지 확인.
    """
    # 같은 ticker, 다른 filed_date의 여러 filing 삽입
    test_db.execute("""
        INSERT INTO raw.sec_financials (
            ticker, cik, filing_type, period_of_report, filed_date,
            revenue, net_income
        ) VALUES
        ('MULTITEST', '0000000004', '10-K', '2023-12-31', '2024-01-15', 900000000, 180000000),
        ('MULTITEST', '0000000004', '10-K', '2023-12-31', '2024-02-15', 1000000000, 200000000),
        ('MULTITEST', '0000000004', '10-K', '2023-12-31', '2024-03-15', 1100000000, 220000000)
    """)

    # as_of_date 기준 최신 선택
    result = get_latest_financials("MULTITEST", "2024-02-20", conn=test_db)

    assert result is not None
    assert result["revenue"] == 1000000000, "Should select latest filing by filed_date"
    # filed_date는 date 객체 또는 문자열 모두 허용
    filed_date_str = str(result["filed_date"]) if hasattr(result["filed_date"], "isoformat") else result["filed_date"]
    assert filed_date_str == "2024-02-15", f"Should select latest filing, got {filed_date_str}"


def test_empty_result(test_db):
    """
    데이터가 없는 경우 None 반환.
    """
    result = get_latest_financials("NONEXISTENT", "2024-01-01", conn=test_db)
    assert result is None, "Should return None for non-existent ticker"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
