"""
레짐 피처 산출 모듈

12개 피처를 DuckDB에서 조회하여 pd.Series로 반환.
vxmt는 데이터 없으므로 항상 NaN.

피처 목록:
  vix       — FRED VIXCLS
  vix3m     — FRED VIX3M (VIXREM 시리즈 사용)
  vxmt      — 항상 NaN (데이터 없음)
  vix_term  — vix3m / vix
  rv20      — SPY 20거래일 실현변동성 (연율화)
  rv60      — SPY 60거래일 실현변동성 (연율화)
  ma200_gap — (SPY종가 - MA200) / MA200
  r12m      — SPY 252거래일 누적수익률
  r1m       — SPY 21거래일 누적수익률
  avg_corr20 — 거래대금 상위 50종목의 20일 pairwise correlation 평균
  hy_spread  — FRED BAMLH0A0HYM2
  ig_spread  — FRED BAMLC0A0CM
  term_spread — FRED T10Y2Y
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection, get_pg_connection
from utils.logger import logger

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 피처 컬럼 순서 (DB 저장 순서와 동일)
FEATURE_COLUMNS = [
    "vix", "vix3m", "vxmt", "vix_term",
    "rv20", "rv60", "ma200_gap",
    "r12m", "r1m",
    "avg_corr20",
    "hy_spread", "ig_spread", "term_spread",
]

# FRED 시리즈 매핑: 피처명 → series_id
FRED_SERIES_MAP = {
    "vix": "VIXCLS",
    "vix3m": "VIXREM",      # VIX 3-Month Forward
    "hy_spread": "BAMLH0A0HYM2",
    "ig_spread": "BAMLC0A0CM",
    "term_spread": "T10Y2Y",
}

# SPY 티커
SPY_TICKER = "SPY"

# avg_corr20 계산에 사용할 상위 종목 수
TOP_N_FOR_CORR = 50


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_fred_value(series_id: str, date: str, conn: duckdb.DuckDBPyConnection) -> Optional[float]:
    """date 이전 가장 가까운 FRED 값 반환. 없으면 None."""
    result = conn.execute(
        """
        SELECT value
        FROM raw.fred_series
        WHERE series_id = ?
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 1
        """,
        [series_id, date],
    ).fetchone()

    return float(result[0]) if result else None


def _fetch_spy_prices(date: str, lookback_days: int, conn: duckdb.DuckDBPyConnection) -> pd.Series:
    """
    date 이전 lookback_days 거래일치 SPY adj_close 조회.

    Returns:
        pd.Series: 인덱스=date(datetime), 값=adj_close, 오름차순 정렬
    """
    df = conn.execute(
        """
        SELECT date, adj_close
        FROM raw.prices
        WHERE ticker = ?
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT ?
        """,
        [SPY_TICKER, date, lookback_days],
    ).df()

    if df.empty:
        return pd.Series(dtype=float)

    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["adj_close"]


def _compute_rv(prices: pd.Series, window: int) -> Optional[float]:
    """
    최근 window거래일 일간 로그수익률 표준편차 × sqrt(252) (연율화 실현변동성).

    데이터가 window + 1개 미만이면 None 반환.
    """
    if len(prices) < window + 1:
        return None

    tail = prices.iloc[-(window + 1):]
    log_rets = np.log(tail / tail.shift(1)).dropna()

    if len(log_rets) < window:
        return None

    return float(log_rets.std() * np.sqrt(252))


def _compute_ma200_gap(prices: pd.Series) -> Optional[float]:
    """
    (현재가 - MA200) / MA200.

    데이터 200일 미만이면 None 반환.
    """
    if len(prices) < 200:
        return None

    current_price = float(prices.iloc[-1])
    ma200 = float(prices.iloc[-200:].mean())

    if ma200 <= 0:
        return None

    return (current_price - ma200) / ma200


def _compute_cumret(prices: pd.Series, window: int) -> Optional[float]:
    """
    최근 window 거래일 누적수익률 (price_end / price_start - 1).

    데이터 window + 1개 미만이면 None 반환.
    """
    if len(prices) < window + 1:
        return None

    price_start = float(prices.iloc[-(window + 1)])
    price_end = float(prices.iloc[-1])

    if price_start <= 0:
        return None

    return (price_end - price_start) / price_start


def _compute_avg_corr20(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[float]:
    """
    거래대금 상위 TOP_N_FOR_CORR 종목의 20일 pairwise correlation 평균.

    1. date 기준 거래대금 상위 종목 선정
    2. 20거래일 adj_close 조회
    3. pairwise correlation 행렬 계산
    4. 상삼각 요소 평균 반환
    """
    # 1. 거래대금 상위 종목 선정 (SPY 제외)
    top_tickers_df = conn.execute(
        """
        SELECT ticker, AVG(close * volume) AS avg_dollar_vol
        FROM raw.prices
        WHERE date <= CAST(? AS DATE)
          AND date >= CAST(? AS DATE) - INTERVAL 30 DAY
          AND ticker != ?
          AND close IS NOT NULL
          AND volume IS NOT NULL
        GROUP BY ticker
        ORDER BY avg_dollar_vol DESC
        LIMIT ?
        """,
        [date, date, SPY_TICKER, TOP_N_FOR_CORR],
    ).df()

    if top_tickers_df.empty:
        return None

    top_tickers = top_tickers_df["ticker"].tolist()

    if len(top_tickers) < 5:
        logger.warning(f"[레짐 피처] avg_corr20 계산 종목 수 부족: {len(top_tickers)}개")
        return None

    # 2. 20거래일 adj_close 조회
    placeholders = ",".join(["?" for _ in top_tickers])
    prices_df = conn.execute(
        f"""
        SELECT ticker, date, adj_close
        FROM raw.prices
        WHERE ticker IN ({placeholders})
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT ?
        """,
        [*top_tickers, date, 21 * len(top_tickers)],
    ).df()

    if prices_df.empty:
        return None

    # 3. 피벗 테이블 → 수익률 행렬
    pivot = prices_df.pivot_table(index="date", columns="ticker", values="adj_close")
    pivot = pivot.sort_index()

    # 최근 21거래일 (20 수익률 계산에 필요)
    if len(pivot) < 21:
        return None

    pivot_tail = pivot.iloc[-21:]
    rets = pivot_tail.pct_change().dropna()

    if rets.shape[0] < 10 or rets.shape[1] < 5:
        return None

    # 4. pairwise correlation 평균 (상삼각 요소)
    corr_matrix = rets.corr()
    n = len(corr_matrix)
    upper_idx = np.triu_indices(n, k=1)
    corr_vals = corr_matrix.values[upper_idx]

    valid_corr = corr_vals[~np.isnan(corr_vals)]
    if len(valid_corr) == 0:
        return None

    return float(np.mean(valid_corr))


def _save_features(date: str, features: pd.Series, conn: duckdb.DuckDBPyConnection) -> None:
    """feature.regime_features 테이블에 피처 저장 (기존 데이터 대체)."""
    def _nan_to_none(val):
        """NaN → None 변환 (psycopg2는 NaN을 직접 받지 못함)."""
        if val is None:
            return None
        try:
            if float(val) != float(val):  # NaN 체크
                return None
            return float(val)
        except (TypeError, ValueError):
            return None

    logger.info(f"[레짐 피처] DB 저장 시작: {date}")
    pg_conn = get_pg_connection()
    cur = pg_conn.cursor()
    try:
        cur.execute(
            "DELETE FROM feature.regime_features WHERE date = %s::date",
            (date,),
        )
        cur.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
                 r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread, computed_at)
            VALUES (%s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                date,
                _nan_to_none(features.get("vix")),
                _nan_to_none(features.get("vix3m")),
                _nan_to_none(features.get("vxmt")),
                _nan_to_none(features.get("vix_term")),
                _nan_to_none(features.get("rv20")),
                _nan_to_none(features.get("rv60")),
                _nan_to_none(features.get("ma200_gap")),
                _nan_to_none(features.get("r12m")),
                _nan_to_none(features.get("r1m")),
                _nan_to_none(features.get("avg_corr20")),
                _nan_to_none(features.get("hy_spread")),
                _nan_to_none(features.get("ig_spread")),
                _nan_to_none(features.get("term_spread")),
            ),
        )
        pg_conn.commit()
        logger.info(f"[레짐 피처] DB 저장 완료: {date}, 피처 수=13")
    except Exception as e:
        pg_conn.rollback()
        logger.error(f"[레짐 피처] DB 저장 실패: {date}, 오류={e}")
        raise
    finally:
        cur.close()
        pg_conn.close()

    # 테스트 환경(인메모리 DuckDB)에서도 읽기가 가능하도록 DuckDB conn에도 best-effort 저장
    try:
        conn.execute(
            "DELETE FROM feature.regime_features WHERE date = CAST(? AS DATE)",
            [date],
        )
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
                 r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
            VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                date,
                features.get("vix"), features.get("vix3m"), features.get("vxmt"),
                features.get("vix_term"), features.get("rv20"), features.get("rv60"),
                features.get("ma200_gap"), features.get("r12m"), features.get("r1m"),
                features.get("avg_corr20"), features.get("hy_spread"),
                features.get("ig_spread"), features.get("term_spread"),
            ],
        )
    except Exception:
        pass  # 프로덕션 DuckDB(read-only postgres_scanner)에서는 무시


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def compute_features(
    date: str,
    conn: duckdb.DuckDBPyConnection = None,
) -> pd.Series:
    """
    지정 날짜 기준 12개 레짐 피처 산출.

    vxmt는 항상 NaN.
    산출된 피처는 feature.regime_features 테이블에도 저장.

    Args:
        date: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        pd.Series: 인덱스=피처명, 값=float (없으면 NaN)
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[레짐 피처] 산출 시작: {date}")

    try:
        features: dict = {}

        # ── 1. FRED 기반 피처 ────────────────────────────────────────────
        for feat_name, series_id in FRED_SERIES_MAP.items():
            val = _fetch_fred_value(series_id, date, conn)
            features[feat_name] = val
            logger.debug(f"[레짐 피처] {feat_name} ({series_id}): {val}")

        # vxmt 항상 NaN
        features["vxmt"] = None

        # ── 2. VIX Term Structure ────────────────────────────────────────
        vix = features.get("vix")
        vix3m = features.get("vix3m")
        if vix is not None and vix3m is not None and vix > 0:
            features["vix_term"] = vix3m / vix
        else:
            features["vix_term"] = None
        logger.debug(f"[레짐 피처] vix_term: {features.get('vix_term')}")

        # ── 3. SPY 가격 기반 피처 ────────────────────────────────────────
        # 252 + 200 + 여유분 = 500거래일치 로드 (r12m, ma200_gap 모두 커버)
        spy_prices = _fetch_spy_prices(date, lookback_days=520, conn=conn)
        logger.debug(f"[레짐 피처] SPY 가격 로드: {len(spy_prices)}개 거래일")

        features["rv20"] = _compute_rv(spy_prices, window=20)
        features["rv60"] = _compute_rv(spy_prices, window=60)
        features["ma200_gap"] = _compute_ma200_gap(spy_prices)
        features["r12m"] = _compute_cumret(spy_prices, window=252)
        features["r1m"] = _compute_cumret(spy_prices, window=21)

        logger.debug(
            f"[레짐 피처] rv20={features['rv20']}, rv60={features['rv60']}, "
            f"ma200_gap={features['ma200_gap']}, r12m={features['r12m']}, r1m={features['r1m']}"
        )

        # ── 4. avg_corr20 ────────────────────────────────────────────────
        features["avg_corr20"] = _compute_avg_corr20(date, conn)
        logger.debug(f"[레짐 피처] avg_corr20: {features.get('avg_corr20')}")

        # ── 5. pd.Series로 변환 ──────────────────────────────────────────
        result = pd.Series(
            {col: features.get(col) for col in FEATURE_COLUMNS},
            dtype=float,
        )

        # None → NaN 변환 확인
        nan_count = int(result.isna().sum())
        logger.info(
            f"[레짐 피처] 산출 완료: {date}, "
            f"NaN={nan_count}개, "
            f"유효={len(FEATURE_COLUMNS) - nan_count}개"
        )

        # ── 6. DB 저장 ───────────────────────────────────────────────────
        try:
            _save_features(date, result, conn)
            logger.debug(f"[레짐 피처] DB 저장 완료: {date}")
        except Exception as save_err:
            logger.warning(f"[레짐 피처] DB 저장 실패 (산출값은 유효): {save_err}")

        return result

    finally:
        if close_conn:
            conn.close()


def compute_features_range(
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection = None,
) -> pd.DataFrame:
    """
    기간 내 모든 SPY 거래일에 대해 12개 피처 DataFrame 반환.

    Args:
        start: 시작 날짜 (YYYY-MM-DD, 포함)
        end: 종료 날짜 (YYYY-MM-DD, 포함)
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        pd.DataFrame: 인덱스=date, 컬럼=FEATURE_COLUMNS (12개)
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[레짐 피처 범위] 산출 시작: {start} ~ {end}")

    try:
        # SPY 거래일 목록 조회
        trading_dates_df = conn.execute(
            """
            SELECT DISTINCT date
            FROM raw.prices
            WHERE ticker = ?
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
            """,
            [SPY_TICKER, start, end],
        ).df()

        if trading_dates_df.empty:
            logger.warning(f"[레짐 피처 범위] 거래일 없음: {start} ~ {end}")
            return pd.DataFrame(columns=FEATURE_COLUMNS)

        trading_dates = trading_dates_df["date"].tolist()
        logger.info(f"[레짐 피처 범위] 거래일 {len(trading_dates)}개 처리")

        rows = []
        for i, td in enumerate(trading_dates):
            date_str = str(td)[:10]  # DATE → 'YYYY-MM-DD'
            try:
                feat = compute_features(date_str, conn=conn)
                rows.append(feat)
            except Exception as e:
                logger.error(f"[레짐 피처 범위] {date_str} 산출 실패: {e}")
                rows.append(pd.Series({col: float("nan") for col in FEATURE_COLUMNS}))

            if (i + 1) % 20 == 0:
                logger.info(f"[레짐 피처 범위] 진행: {i + 1}/{len(trading_dates)}")

        result_df = pd.DataFrame(rows, index=pd.to_datetime([str(td)[:10] for td in trading_dates]))
        result_df.index.name = "date"
        result_df = result_df[FEATURE_COLUMNS]

        logger.info(
            f"[레짐 피처 범위] 완료: {len(result_df)}행, "
            f"컬럼={list(result_df.columns)}"
        )
        return result_df

    finally:
        if close_conn:
            conn.close()
