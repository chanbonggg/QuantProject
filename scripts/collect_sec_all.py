"""
S&P500 전체 503개 티커에 대해 SEC EDGAR 재무데이터를 수집하는 스크립트.
SEC Rate Limit (0.12초 간격) 준수하며 증분 수집.
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from data.collectors.sec_collector import collect_financials
from db.init import get_connection
from utils.logger import logger

# S&P500 전체 티커 리스트
sys.path.insert(0, str(Path(__file__).parent.parent))
from sp500_tickers_list import SP500_TICKERS


def main():
    conn = get_connection()

    # 이미 수집된 티커 확인
    existing = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM raw.sec_financials"
        ).fetchall()
    )
    logger.info(f"[SEC 전체수집] 이미 수집된 티커: {len(existing)}개")

    remaining = [t for t in SP500_TICKERS if t not in existing]
    logger.info(f"[SEC 전체수집] 수집 대상: {len(remaining)}개 티커")

    total_rows = 0
    success = 0
    failed = []

    for i, ticker in enumerate(remaining, 1):
        try:
            n = collect_financials(ticker, start_year=2015, conn=conn)
            total_rows += n
            if n > 0:
                success += 1
            if i % 20 == 0:
                logger.info(
                    f"[SEC 전체수집] 진행: {i}/{len(remaining)} "
                    f"(성공={success}, 실패={len(failed)}, 총행={total_rows})"
                )
        except Exception as e:
            logger.error(f"[SEC 전체수집] {ticker} 실패: {e}")
            failed.append(ticker)

        # SEC Rate Limit 추가 안전장치
        time.sleep(0.15)

    conn.close()

    logger.info(
        f"[SEC 전체수집] 완료! "
        f"성공={success}, 실패={len(failed)}, 총행={total_rows}"
    )
    if failed:
        logger.warning(f"[SEC 전체수집] 실패 티커: {failed}")


if __name__ == "__main__":
    main()
