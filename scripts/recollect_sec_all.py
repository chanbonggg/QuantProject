"""
SEC 데이터 재수집 (대체 XBRL 태그 반영).
기존 데이터 삭제 후 전체 재수집.
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from data.collectors.sec_collector import collect_financials
from db.init import get_connection
from utils.logger import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from sp500_tickers_list import SP500_TICKERS


def main():
    conn = get_connection()

    # 기존 데이터 삭제
    conn.execute("DELETE FROM raw.sec_financials")
    conn.commit()
    logger.info("[SEC 재수집] 기존 데이터 삭제 완료")

    total_rows = 0
    success = 0
    failed = []

    for i, ticker in enumerate(SP500_TICKERS, 1):
        try:
            n = collect_financials(ticker, start_year=2015, conn=conn)
            total_rows += n
            if n > 0:
                success += 1
            if i % 50 == 0:
                logger.info(
                    f"[SEC 재수집] 진행: {i}/{len(SP500_TICKERS)} "
                    f"(성공={success}, 실패={len(failed)}, 총행={total_rows})"
                )
        except Exception as e:
            logger.error(f"[SEC 재수집] {ticker} 실패: {e}")
            failed.append(ticker)

        time.sleep(0.15)

    conn.close()

    logger.info(
        f"[SEC 재수집] 완료! "
        f"성공={success}, 실패={len(failed)}, 총행={total_rows}"
    )
    if failed:
        logger.warning(f"[SEC 재수집] 실패 티커: {failed}")


if __name__ == "__main__":
    main()
