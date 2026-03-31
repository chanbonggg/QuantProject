"""
DuckDB → PostgreSQL 데이터 마이그레이션 스크립트

이전 대상:
  - raw.prices           (777,094행)
  - raw.fred_series      (16,373행)
  - raw.sec_financials   (38,859행)
  - feature.regime_features
  - feature.regime_labels

실행:
  python quant_us/scripts/migrate_duckdb_to_pg.py
  python quant_us/scripts/migrate_duckdb_to_pg.py --dry-run  # 건수만 확인
"""

import sys
import argparse
from pathlib import Path
from typing import Optional

import duckdb
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_pg_connection
from utils.logger import logger

DUCKDB_PATH = Path(__file__).parent.parent.parent / "data" / "quant_us.duckdb"
BATCH_SIZE = 5_000


def _migrate_prices(duck: duckdb.DuckDBPyConnection, dry_run: bool) -> int:
    logger.info("[마이그레이션] raw.prices 시작")
    total = duck.execute("SELECT COUNT(*) FROM raw.prices").fetchone()[0]
    logger.info(f"[마이그레이션] raw.prices 총 {total:,}행")

    if dry_run:
        return total

    pg = get_pg_connection()
    cur = pg.cursor()
    inserted = 0
    offset = 0

    try:
        while offset < total:
            rows = duck.execute(f"""
                SELECT ticker, date, open, high, low, close, adj_close,
                       volume, market_cap, source, collected_at
                FROM raw.prices
                ORDER BY ticker, date
                LIMIT {BATCH_SIZE} OFFSET {offset}
            """).fetchall()

            if not rows:
                break

            execute_values(cur, """
                INSERT INTO raw.prices
                    (ticker, date, open, high, low, close, adj_close,
                     volume, market_cap, source, collected_at)
                VALUES %s
                ON CONFLICT (ticker, date) DO NOTHING
            """, rows)
            pg.commit()

            inserted += len(rows)
            offset += BATCH_SIZE
            logger.info(f"[마이그레이션] raw.prices 진행: {inserted:,}/{total:,}")

    except Exception as e:
        pg.rollback()
        logger.error(f"[마이그레이션] raw.prices 실패: {e}")
        raise
    finally:
        cur.close()
        pg.close()

    logger.info(f"[마이그레이션] raw.prices 완료: {inserted:,}행")
    return inserted


def _migrate_fred(duck: duckdb.DuckDBPyConnection, dry_run: bool) -> int:
    logger.info("[마이그레이션] raw.fred_series 시작")
    total = duck.execute("SELECT COUNT(*) FROM raw.fred_series").fetchone()[0]
    logger.info(f"[마이그레이션] raw.fred_series 총 {total:,}행")

    if dry_run:
        return total

    pg = get_pg_connection()
    cur = pg.cursor()
    inserted = 0
    offset = 0

    try:
        while offset < total:
            rows = duck.execute(f"""
                SELECT series_id, date, value, collected_at
                FROM raw.fred_series
                ORDER BY series_id, date
                LIMIT {BATCH_SIZE} OFFSET {offset}
            """).fetchall()

            if not rows:
                break

            execute_values(cur, """
                INSERT INTO raw.fred_series (series_id, date, value, collected_at)
                VALUES %s
                ON CONFLICT (series_id, date) DO NOTHING
            """, rows)
            pg.commit()

            inserted += len(rows)
            offset += BATCH_SIZE
            logger.info(f"[마이그레이션] raw.fred_series 진행: {inserted:,}/{total:,}")

    except Exception as e:
        pg.rollback()
        logger.error(f"[마이그레이션] raw.fred_series 실패: {e}")
        raise
    finally:
        cur.close()
        pg.close()

    logger.info(f"[마이그레이션] raw.fred_series 완료: {inserted:,}행")
    return inserted


def _migrate_sec(duck: duckdb.DuckDBPyConnection, dry_run: bool) -> int:
    logger.info("[마이그레이션] raw.sec_financials 시작")
    total = duck.execute("SELECT COUNT(*) FROM raw.sec_financials").fetchone()[0]
    logger.info(f"[마이그레이션] raw.sec_financials 총 {total:,}행")

    if dry_run:
        return total

    pg = get_pg_connection()
    cur = pg.cursor()
    inserted = 0
    offset = 0

    try:
        while offset < total:
            rows = duck.execute(f"""
                SELECT ticker, cik, filing_type, period_of_report, filed_date,
                       revenue, net_income, eps_diluted, total_assets,
                       stockholders_equity, total_liabilities, operating_cashflow,
                       cost_of_goods_sold, collected_at
                FROM raw.sec_financials
                ORDER BY ticker, filed_date
                LIMIT {BATCH_SIZE} OFFSET {offset}
            """).fetchall()

            if not rows:
                break

            execute_values(cur, """
                INSERT INTO raw.sec_financials
                    (ticker, cik, filing_type, period_of_report, filed_date,
                     revenue, net_income, eps_diluted, total_assets,
                     stockholders_equity, total_liabilities, operating_cashflow,
                     cost_of_goods_sold, collected_at)
                VALUES %s
                ON CONFLICT (ticker, filed_date, filing_type) DO NOTHING
            """, rows)
            pg.commit()

            inserted += len(rows)
            offset += BATCH_SIZE
            logger.info(f"[마이그레이션] raw.sec_financials 진행: {inserted:,}/{total:,}")

    except Exception as e:
        pg.rollback()
        logger.error(f"[마이그레이션] raw.sec_financials 실패: {e}")
        raise
    finally:
        cur.close()
        pg.close()

    logger.info(f"[마이그레이션] raw.sec_financials 완료: {inserted:,}행")
    return inserted


def _migrate_regime_features(duck: duckdb.DuckDBPyConnection, dry_run: bool) -> int:
    logger.info("[마이그레이션] feature.regime_features 시작")
    try:
        total = duck.execute("SELECT COUNT(*) FROM feature.regime_features").fetchone()[0]
    except Exception:
        logger.info("[마이그레이션] feature.regime_features 없음 — 스킵")
        return 0

    logger.info(f"[마이그레이션] feature.regime_features 총 {total:,}행")
    if dry_run:
        return total

    pg = get_pg_connection()
    cur = pg.cursor()
    inserted = 0
    offset = 0

    try:
        while offset < total:
            rows = duck.execute(f"""
                SELECT date, vix, vix3m, vxmt, vix_term, rv20, rv60,
                       ma200_gap, r12m, r1m, avg_corr20, hy_spread,
                       ig_spread, term_spread, computed_at
                FROM feature.regime_features
                ORDER BY date
                LIMIT {BATCH_SIZE} OFFSET {offset}
            """).fetchall()

            if not rows:
                break

            execute_values(cur, """
                INSERT INTO feature.regime_features
                    (date, vix, vix3m, vxmt, vix_term, rv20, rv60,
                     ma200_gap, r12m, r1m, avg_corr20, hy_spread,
                     ig_spread, term_spread, computed_at)
                VALUES %s
                ON CONFLICT (date) DO NOTHING
            """, rows)
            pg.commit()

            inserted += len(rows)
            offset += BATCH_SIZE

    except Exception as e:
        pg.rollback()
        logger.error(f"[마이그레이션] feature.regime_features 실패: {e}")
        raise
    finally:
        cur.close()
        pg.close()

    logger.info(f"[마이그레이션] feature.regime_features 완료: {inserted:,}행")
    return inserted


def _migrate_regime_labels(duck: duckdb.DuckDBPyConnection, dry_run: bool) -> int:
    logger.info("[마이그레이션] feature.regime_labels 시작")
    try:
        total = duck.execute("SELECT COUNT(*) FROM feature.regime_labels").fetchone()[0]
    except Exception:
        logger.info("[마이그레이션] feature.regime_labels 없음 — 스킵")
        return 0

    logger.info(f"[마이그레이션] feature.regime_labels 총 {total:,}행")
    if dry_run:
        return total

    # raw_regime 컬럼 존재 여부 확인
    cols = [c[0] for c in duck.execute("DESCRIBE feature.regime_labels").fetchall()]
    has_raw_regime = "raw_regime" in cols

    pg = get_pg_connection()
    cur = pg.cursor()
    inserted = 0
    offset = 0

    try:
        while offset < total:
            if has_raw_regime:
                rows = duck.execute(f"""
                    SELECT date, regime, shock_alarm, raw_regime, computed_at
                    FROM feature.regime_labels
                    ORDER BY date
                    LIMIT {BATCH_SIZE} OFFSET {offset}
                """).fetchall()
                execute_values(cur, """
                    INSERT INTO feature.regime_labels
                        (date, regime, shock_alarm, raw_regime, computed_at)
                    VALUES %s
                    ON CONFLICT (date) DO NOTHING
                """, rows)
            else:
                rows = duck.execute(f"""
                    SELECT date, regime, shock_alarm, computed_at
                    FROM feature.regime_labels
                    ORDER BY date
                    LIMIT {BATCH_SIZE} OFFSET {offset}
                """).fetchall()
                execute_values(cur, """
                    INSERT INTO feature.regime_labels
                        (date, regime, shock_alarm, computed_at)
                    VALUES %s
                    ON CONFLICT (date) DO NOTHING
                """, rows)

            if not rows:
                break

            pg.commit()
            inserted += len(rows)
            offset += BATCH_SIZE

    except Exception as e:
        pg.rollback()
        logger.error(f"[마이그레이션] feature.regime_labels 실패: {e}")
        raise
    finally:
        cur.close()
        pg.close()

    logger.info(f"[마이그레이션] feature.regime_labels 완료: {inserted:,}행")
    return inserted


def run_migration(dry_run: bool = False) -> None:
    if not DUCKDB_PATH.exists():
        logger.error(f"DuckDB 파일 없음: {DUCKDB_PATH}")
        sys.exit(1)

    logger.info(f"[마이그레이션] 시작 — DuckDB: {DUCKDB_PATH}, dry_run={dry_run}")
    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    try:
        results = {
            "raw.prices":              _migrate_prices(duck, dry_run),
            "raw.fred_series":         _migrate_fred(duck, dry_run),
            "raw.sec_financials":      _migrate_sec(duck, dry_run),
            "feature.regime_features": _migrate_regime_features(duck, dry_run),
            "feature.regime_labels":   _migrate_regime_labels(duck, dry_run),
        }
    finally:
        duck.close()

    logger.info("[마이그레이션] === 결과 요약 ===")
    for table, cnt in results.items():
        label = "확인" if dry_run else "이전"
        logger.info(f"  {table}: {cnt:,}행 {label}")

    if dry_run:
        logger.info("[마이그레이션] dry-run 완료 — 실제 이전 없음")
    else:
        # PostgreSQL 최종 확인
        pg = get_pg_connection()
        cur = pg.cursor()
        logger.info("[마이그레이션] === PostgreSQL 최종 확인 ===")
        for table in results:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            cnt = cur.fetchone()[0]
            logger.info(f"  {table}: {cnt:,}행")
        cur.close()
        pg.close()
        logger.info("[마이그레이션] 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DuckDB → PostgreSQL 데이터 마이그레이션")
    parser.add_argument("--dry-run", action="store_true", help="건수만 확인, 실제 이전 없음")
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run)
