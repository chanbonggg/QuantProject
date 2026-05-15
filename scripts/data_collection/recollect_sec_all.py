"""
SEC 데이터 재수집 (대체 XBRL 태그 반영).
기존 데이터 삭제 후 전체 재수집.
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "quant_us"))

from data.collectors.sec_collector import collect_financials
from db.init import get_pg_connection
from utils.logger import logger

from data.collectors.price_collector import SP500_TICKERS


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SEC 재무 데이터를 전체 삭제 후 재수집합니다.")
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="raw.sec_financials 기존 데이터를 삭제하고 재수집합니다.",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    conn = get_pg_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM raw.sec_financials")
    existing_count = cur.fetchone()[0]
    logger.warning(f"[SEC 재수집] 기존 raw.sec_financials 행 수: {existing_count:,}")

    if not args.confirm_delete:
        logger.error("[SEC 재수집] 중단: 삭제 재수집은 --confirm-delete 플래그가 필요합니다.")
        cur.close()
        conn.close()
        return

    cur.execute("DELETE FROM raw.sec_financials")
    conn.commit()
    logger.info("[SEC 재수집] 기존 데이터 삭제 완료")
    cur.close()
    conn.close()

    total_rows = 0
    success = 0
    failed = []

    for i, ticker in enumerate(SP500_TICKERS, 1):
        try:
            n = collect_financials(ticker, start_year=2015)
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

    logger.info(
        f"[SEC 재수집] 완료! "
        f"성공={success}, 실패={len(failed)}, 총행={total_rows}"
    )
    if failed:
        logger.warning(f"[SEC 재수집] 실패 티커: {failed}")


if __name__ == "__main__":
    main()
