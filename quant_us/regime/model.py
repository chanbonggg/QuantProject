"""
레짐 판단 모델

1단계: 퍼센타일 기반 규칙 (메인)
  - Regime B (Risk-off): VIX >= 25 AND rv20 >= 80pctl AND ma200_gap < 0 AND vix_term < 1.0
  - Regime A (Risk-on): VIX < 20 AND ma200_gap > 0 AND r12m > 0 AND hy_spread < 60pctl
  - Regime C: 나머지

2단계: 히스테리시스 (진입 2일, 이탈 3일 연속)

보조: GaussianHMM (3-state) — 모델 파일 없으면 규칙 기반만 사용
"""

import sys
from datetime import datetime, date, timedelta
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

DEFAULT_MODEL_PATH = str(
    Path(__file__).parent.parent.parent / "data" / "models" / "regime_hmm.pkl"
)

# HMM 입력 피처 (FRED 기반 3개 — 데이터 가용성 최우선)
HMM_FEATURES = ["vix", "hy_spread", "term_spread"]

# 퍼센타일 임계값
PCT_RV20_B = 80       # Regime B 진입: rv20 >= 80pctl
PCT_HY_A = 60         # Regime A 진입: hy_spread < 60pctl

# 히스테리시스 임계값
ENTRY_THRESHOLD = 2   # 진입: 2일 연속
EXIT_THRESHOLD = 3    # 이탈: 3일 연속

# 퍼센타일 계산 기준 거래일 (약 5년)
LOOKBACK_DAYS = 1260


# ---------------------------------------------------------------------------
# 히스테리시스 상태 관리
# ---------------------------------------------------------------------------

class RegimeState:
    """
    레짐 히스테리시스 상태 추적.

    current_regime: 현재 확정된 레짐 ('A'|'B'|'C')
    candidate_regime: 전환 후보 레짐
    consecutive_days: 후보 연속 일수
    """

    def __init__(self, initial_regime: str = "C") -> None:
        self.current_regime: str = initial_regime
        self.candidate_regime: Optional[str] = None
        self.consecutive_days: int = 0

    def update(self, raw_regime: str, date_str: str = "") -> str:
        """
        히스테리시스 적용 후 최종 레짐 반환.

        Args:
            raw_regime: 규칙 기반으로 산출한 원시 레짐
            date_str: 로그용 날짜 문자열

        Returns:
            str: 히스테리시스 적용 후 최종 레짐 ('A'|'B'|'C')
        """
        if raw_regime == self.current_regime:
            # 현재 레짐과 동일 → 후보 초기화, 유지
            self.candidate_regime = None
            self.consecutive_days = 0
            return self.current_regime

        # 다른 레짐이 관측됨
        if raw_regime == self.candidate_regime:
            self.consecutive_days += 1
        else:
            # 새 후보로 교체
            self.candidate_regime = raw_regime
            self.consecutive_days = 1

        # 임계값 결정: 최초 진입(C→A/B)은 ENTRY_THRESHOLD, 이탈(A/B→X)은 EXIT_THRESHOLD
        if self.current_regime == "C":
            threshold = ENTRY_THRESHOLD
        else:
            threshold = EXIT_THRESHOLD

        if self.consecutive_days >= threshold:
            prev = self.current_regime
            self.current_regime = raw_regime
            self.candidate_regime = None
            self.consecutive_days = 0
            logger.info(
                f"[레짐 히스테리시스] 전환: {prev} → {self.current_regime} "
                f"(기준일: {date_str}, 임계값: {threshold}일)"
            )

        return self.current_regime

    def to_dict(self) -> dict:
        """직렬화."""
        return {
            "current_regime": self.current_regime,
            "candidate_regime": self.candidate_regime,
            "consecutive_days": self.consecutive_days,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegimeState":
        """역직렬화."""
        state = cls(initial_regime=data.get("current_regime", "C"))
        state.candidate_regime = data.get("candidate_regime")
        state.consecutive_days = data.get("consecutive_days", 0)
        return state


# ---------------------------------------------------------------------------
# 퍼센타일 계산
# ---------------------------------------------------------------------------

def _compute_percentiles(
    conn: duckdb.DuckDBPyConnection,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict:
    """
    퍼센타일 기준값 계산.

    raw.fred_series에서 VIX, HY_SPREAD 등을 조회.
    feature.regime_features에서 rv20 조회 (있으면).
    없으면 가용 데이터 전체 사용.

    Returns:
        dict: {'rv20': {80: val}, 'hy_spread': {60: val}, ...}
    """
    logger.debug(f"[레짐 모델] 퍼센타일 계산 시작 (lookback={lookback_days}일)")
    percentiles: dict = {}

    # --- VIX 퍼센타일 (raw.fred_series) ---
    try:
        vix_rows = conn.execute(
            """
            SELECT value FROM raw.fred_series
            WHERE series_id = 'VIXCLS'
            ORDER BY date DESC
            LIMIT ?
            """,
            [lookback_days],
        ).fetchall()

        if vix_rows:
            vix_vals = np.array([r[0] for r in vix_rows if r[0] is not None])
            if len(vix_vals) > 0:
                percentiles["vix"] = {
                    80: float(np.percentile(vix_vals, 80)),
                    60: float(np.percentile(vix_vals, 60)),
                    20: float(np.percentile(vix_vals, 20)),
                }
    except Exception as e:
        logger.warning(f"[레짐 모델] VIX 퍼센타일 계산 실패: {e}")

    # --- HY 스프레드 퍼센타일 (raw.fred_series) ---
    try:
        hy_rows = conn.execute(
            """
            SELECT value FROM raw.fred_series
            WHERE series_id = 'BAMLH0A0HYM2'
            ORDER BY date DESC
            LIMIT ?
            """,
            [lookback_days],
        ).fetchall()

        if hy_rows:
            hy_vals = np.array([r[0] for r in hy_rows if r[0] is not None])
            if len(hy_vals) > 0:
                percentiles["hy_spread"] = {
                    80: float(np.percentile(hy_vals, 80)),
                    60: float(np.percentile(hy_vals, 60)),
                    40: float(np.percentile(hy_vals, 40)),
                }
    except Exception as e:
        logger.warning(f"[레짐 모델] HY 스프레드 퍼센타일 계산 실패: {e}")

    # --- rv20 퍼센타일 (feature.regime_features) ---
    try:
        rv20_rows = conn.execute(
            """
            SELECT rv20 FROM feature.regime_features
            WHERE rv20 IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
            """,
            [lookback_days],
        ).fetchall()

        if rv20_rows:
            rv20_vals = np.array([r[0] for r in rv20_rows if r[0] is not None])
            if len(rv20_vals) > 0:
                percentiles["rv20"] = {
                    80: float(np.percentile(rv20_vals, 80)),
                    60: float(np.percentile(rv20_vals, 60)),
                }
    except Exception as e:
        logger.warning(f"[레짐 모델] rv20 퍼센타일 계산 실패: {e}")

    logger.debug(f"[레짐 모델] 퍼센타일 계산 완료: {list(percentiles.keys())}")
    return percentiles


# ---------------------------------------------------------------------------
# 규칙 기반 레짐 판단
# ---------------------------------------------------------------------------

def _rule_based_regime(features: pd.Series, percentiles: dict) -> str:
    """
    퍼센타일 기반 규칙으로 레짐 판단.

    NaN 피처는 해당 조건 미충족으로 처리 → Regime C 폴백.

    Regime B (Risk-off):
        VIX >= 25 AND rv20 >= 80pctl AND ma200_gap < 0 AND vix_term < 1.0
    Regime A (Risk-on):
        VIX < 20 AND ma200_gap > 0 AND r12m > 0 AND hy_spread < 60pctl

    Args:
        features: compute_features 결과 pd.Series (피처명=인덱스)
        percentiles: _compute_percentiles 결과

    Returns:
        str: 'A' | 'B' | 'C'
    """

    def _safe(val, default=None):
        """NaN이면 default 반환."""
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return default
        return val

    vix = _safe(features.get("vix"))
    rv20 = _safe(features.get("rv20"))
    ma200_gap = _safe(features.get("ma200_gap"))
    vix_term = _safe(features.get("vix_term"))
    r12m = _safe(features.get("r12m"))
    hy_spread = _safe(features.get("hy_spread"))

    # rv20 80pctl 임계값 (없으면 절대값 0.25 폴백)
    rv20_80pctl = percentiles.get("rv20", {}).get(80, 0.25)
    # hy_spread 60pctl 임계값 (없으면 절대값 500bps 폴백)
    hy_60pctl = percentiles.get("hy_spread", {}).get(60, 500.0)

    # --- Regime B 조건 ---
    # NaN 피처는 해당 조건을 무시(True로 처리) — 데이터 부족 시에도 판단 가능
    # 단, 핵심 지표(vix, rv20)는 반드시 유효해야 함
    b_vix = vix is not None and vix >= 25
    b_rv20 = rv20 is not None and rv20 >= rv20_80pctl
    b_ma200 = ma200_gap is None or ma200_gap < 0      # NaN이면 조건 충족으로 처리
    b_vix_term = vix_term is None or vix_term < 1.0   # NaN이면 조건 충족으로 처리

    if b_vix and b_rv20 and b_ma200 and b_vix_term:
        return "B"

    # --- Regime A 조건 ---
    # 핵심(vix, hy_spread)은 필수, 보조(ma200_gap, r12m)는 NaN이면 무시
    a_vix = vix is not None and vix < 20
    a_ma200 = ma200_gap is None or ma200_gap > 0      # NaN이면 조건 충족으로 처리
    a_r12m = r12m is None or r12m > 0                 # NaN이면 조건 충족으로 처리
    a_hy = hy_spread is not None and hy_spread < hy_60pctl

    if a_vix and a_ma200 and a_r12m and a_hy:
        return "A"

    return "C"


# ---------------------------------------------------------------------------
# 히스테리시스 상태 복원
# ---------------------------------------------------------------------------

def _restore_regime_state(conn: duckdb.DuckDBPyConnection, date_str: str) -> RegimeState:
    """
    feature.regime_labels에서 히스테리시스 상태 복원.

    알고리즘:
    1. 전체 이력 중 EXIT_THRESHOLD+5일 이내를 오래된 순으로 조회
    2. raw_regime 컬럼을 순서대로 replay해 candidate/consecutive 복원
    3. 전환 시점마다 DB 저장값(확정 레짐)으로 current 보정

    raw_regime 컬럼 없으면 최근 확정 레짐만 복원.
    이력 없으면 기본 상태(C) 반환.
    """
    lookback = EXIT_THRESHOLD + 5

    try:
        # raw_regime 컬럼 포함 조회 시도 — 최근 N행을 오래된 순으로 반환
        try:
            rows = conn.execute(
                """
                SELECT date, regime, raw_regime FROM (
                    SELECT date, regime, raw_regime FROM feature.regime_labels
                    WHERE date < CAST(? AS DATE)
                    ORDER BY date DESC
                    LIMIT ?
                ) ORDER BY date ASC
                """,
                [date_str, lookback],
            ).fetchall()
            has_raw = True
        except Exception:
            rows = conn.execute(
                """
                SELECT date, regime FROM (
                    SELECT date, regime FROM feature.regime_labels
                    WHERE date < CAST(? AS DATE)
                    ORDER BY date DESC
                    LIMIT ?
                ) ORDER BY date ASC
                """,
                [date_str, lookback],
            ).fetchall()
            has_raw = False

    except Exception as e:
        logger.warning(f"[레짐 모델] 이력 조회 실패: {e}")
        return RegimeState()

    if not rows:
        return RegimeState()

    if not has_raw:
        # raw_regime 없으면 최근 확정 레짐만 복원
        latest_regime = rows[-1][1]
        logger.debug(f"[레짐 모델] raw_regime 없음 — current={latest_regime} 복원")
        return RegimeState(initial_regime=latest_regime)

    # raw_regime을 오래된 순으로 replay
    # 전환이 발생한 시점에서 DB 저장값으로 current를 보정
    state = RegimeState(initial_regime="C")
    for row in rows:
        row_date_str = str(row[0])
        confirmed_regime = row[1]
        raw = row[2] if row[2] else confirmed_regime

        # raw_regime replay
        state.update(raw, row_date_str)

        # 히스테리시스 결과와 DB 저장값이 다르면 DB값으로 강제 보정
        # (다른 프로세스가 레짐을 저장했거나, 첫 실행 등)
        if state.current_regime != confirmed_regime:
            state.current_regime = confirmed_regime
            state.candidate_regime = None
            state.consecutive_days = 0

    logger.debug(
        f"[레짐 모델] 히스테리시스 상태 복원: current={state.current_regime}, "
        f"candidate={state.candidate_regime}, consecutive={state.consecutive_days}, "
        f"이력 {len(rows)}일"
    )
    return state


# ---------------------------------------------------------------------------
# HMM 관련
# ---------------------------------------------------------------------------

def _fit_hmm(features_df: pd.DataFrame, n_states: int = 3):
    """
    GaussianHMM 학습.

    입력 피처: HMM_FEATURES = ['vix', 'hy_spread', 'term_spread']
    NaN 행 dropna 처리.

    Args:
        features_df: compute_features_range 결과 DataFrame
        n_states: HMM 상태 수 (기본 3)

    Returns:
        학습된 GaussianHMM 또는 None (데이터 부족 시)
    """
    try:
        from hmmlearn.hmm import GaussianHMM  # type: ignore
    except ImportError:
        logger.warning("[레짐 모델] hmmlearn 미설치 — HMM 학습 건너뜀")
        return None

    # 피처 선택 및 NaN 제거
    available_features = [f for f in HMM_FEATURES if f in features_df.columns]
    if not available_features:
        logger.warning(f"[레짐 모델] HMM 피처 없음: {HMM_FEATURES}")
        return None

    df_clean = features_df[available_features].dropna()
    if len(df_clean) < 100:
        logger.warning(
            f"[레짐 모델] HMM 학습 데이터 부족: {len(df_clean)}행 (최소 100행 필요)"
        )
        return None

    X = df_clean.values
    logger.info(
        f"[레짐 모델] HMM 학습 시작: {len(X)}행, {len(available_features)}개 피처, "
        f"n_states={n_states}"
    )

    try:
        model = GaussianHMM(
            n_components=n_states,
            covariance_type="full",
            n_iter=200,
            random_state=42,
        )
        model.fit(X)
        logger.info(f"[레짐 모델] HMM 학습 완료: converged={model.monitor_.converged}")
        return model
    except Exception as e:
        logger.error(f"[레짐 모델] HMM 학습 실패: {e}")
        return None


def save_model(model, path: Optional[str] = None) -> None:
    """
    HMM 모델을 joblib으로 저장.

    Args:
        model: 학습된 GaussianHMM 객체
        path: 저장 경로 (None이면 DEFAULT_MODEL_PATH)
    """
    try:
        import joblib  # type: ignore
    except ImportError:
        logger.error("[레짐 모델] joblib 미설치 — 모델 저장 불가")
        return

    save_path = path or DEFAULT_MODEL_PATH
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        joblib.dump(model, save_path)
        logger.info(f"[레짐 모델] 모델 저장 완료: {save_path}")
    except Exception as e:
        logger.error(f"[레짐 모델] 모델 저장 실패: {e}")


def load_model(path: Optional[str] = None):
    """
    joblib으로 HMM 모델 로드.

    모델 파일 없으면 None 반환 (에러 아님).

    Args:
        path: 모델 파일 경로 (None이면 DEFAULT_MODEL_PATH)

    Returns:
        GaussianHMM 또는 None
    """
    try:
        import joblib  # type: ignore
    except ImportError:
        logger.warning("[레짐 모델] joblib 미설치 — 모델 로드 불가")
        return None

    load_path = path or DEFAULT_MODEL_PATH
    if not Path(load_path).exists():
        logger.debug(f"[레짐 모델] 모델 파일 없음: {load_path} (규칙 기반만 사용)")
        return None

    try:
        model = joblib.load(load_path)
        logger.info(f"[레짐 모델] 모델 로드 완료: {load_path}")
        return model
    except Exception as e:
        logger.warning(f"[레짐 모델] 모델 로드 실패: {e}")
        return None


# ---------------------------------------------------------------------------
# HMM 확률 계산
# ---------------------------------------------------------------------------

def _hmm_predict_proba(
    model,
    features: pd.Series,
    regime_map: dict,
) -> Optional[dict]:
    """
    HMM으로 레짐 확률 계산.

    Args:
        model: GaussianHMM 객체
        features: 피처 Series
        regime_map: HMM 상태 → 레짐 매핑 dict (예: {0: 'A', 1: 'B', 2: 'C'})

    Returns:
        dict: {'A': prob, 'B': prob, 'C': prob} 또는 None (실패 시)
    """
    available = [f for f in HMM_FEATURES if f in features.index]
    if not available:
        return None

    x = features[available].values
    if np.any(np.isnan(x)):
        return None

    try:
        # 단일 샘플 예측
        proba_matrix = model.predict_proba(x.reshape(1, -1))
        state_probs = proba_matrix[0]

        result = {"A": 0.0, "B": 0.0, "C": 0.0}
        for state_idx, prob in enumerate(state_probs):
            regime_label = regime_map.get(state_idx, "C")
            result[regime_label] = result.get(regime_label, 0.0) + float(prob)

        return result
    except Exception as e:
        logger.debug(f"[레짐 모델] HMM 확률 계산 실패: {e}")
        return None


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def fit(
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    save_path: Optional[str] = None,
    start: str = "2015-01-01",
    end: Optional[str] = None,
) -> None:
    """
    HMM 모델 학습 및 저장.

    compute_features_range로 학습 데이터 생성 후 GaussianHMM 학습.

    Args:
        conn: DuckDB 연결 (None이면 자동 생성)
        save_path: 저장 경로 (None이면 DEFAULT_MODEL_PATH)
        start: 학습 시작일
        end: 학습 종료일 (None이면 오늘)
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    end_date = end or date.today().strftime("%Y-%m-%d")
    logger.info(f"[레짐 모델] HMM 학습 시작: {start} ~ {end_date}")

    try:
        # features.py에서 피처 데이터 가져오기
        try:
            from regime.features import compute_features_range
            features_df = compute_features_range(start, end_date, conn)
        except ImportError as e:
            logger.warning(f"[레짐 모델] features 모듈 임포트 실패: {e} — feature.regime_features에서 직접 조회")
            features_df = _load_features_from_db(start, end_date, conn)

        if features_df is None or features_df.empty:
            logger.warning("[레짐 모델] 학습 데이터 없음 — HMM 학습 건너뜀")
            return

        model = _fit_hmm(features_df)
        if model is None:
            logger.warning("[레짐 모델] HMM 학습 실패 — 모델 저장 건너뜀")
            return

        save_model(model, save_path)
        logger.info("[레짐 모델] fit() 완료")

    finally:
        if close_conn:
            conn.close()


def predict(
    date_str: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> str:
    """
    레짐 판단: 규칙 기반 + 히스테리시스 적용.

    결과를 feature.regime_labels에 저장.

    Args:
        date_str: 기준 날짜 ('YYYY-MM-DD')
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        str: 'A' | 'B' | 'C'
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[레짐 모델] predict 시작: {date_str}")

    try:
        # 1. 피처 가져오기
        features = _get_features_for_date(date_str, conn)
        if not _has_valid_features(features):
            logger.warning(f"[레짐 모델] {date_str} 피처 없음 → Regime C 반환")
            return "C"

        # 2. 퍼센타일 계산
        percentiles = _compute_percentiles(conn)

        # 3. 규칙 기반 레짐
        raw_regime = _rule_based_regime(features, percentiles)

        # 4. 히스테리시스 복원 및 적용
        state = _restore_regime_state(conn, date_str)
        final_regime = state.update(raw_regime, date_str)

        logger.info(
            f"[레짐 모델] 판단: {date_str} → {final_regime} "
            f"(raw={raw_regime}, 히스테리시스 적용)"
        )

        # 5. feature.regime_labels에 저장 (raw_regime 포함)
        _save_regime_label(date_str, final_regime, conn, raw_regime=raw_regime)

        return final_regime

    finally:
        if close_conn:
            conn.close()


def predict_proba(
    date_str: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict:
    """
    레짐 확률 반환.

    HMM 모델 있으면 HMM 확률 사용.
    없으면 규칙 기반 (해당=0.8, 나머지=0.1씩).

    Args:
        date_str: 기준 날짜 ('YYYY-MM-DD')
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        dict: {'A': float, 'B': float, 'C': float}, 합=1.0
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[레짐 모델] predict_proba 시작: {date_str}")

    try:
        # 1. 피처 가져오기
        features = _get_features_for_date(date_str, conn)
        if not _has_valid_features(features):
            logger.warning(f"[레짐 모델] {date_str} 피처 없음 → 균등 확률 반환")
            return {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}

        # 2. 퍼센타일 계산
        percentiles = _compute_percentiles(conn)

        # 3. 규칙 기반 레짐
        raw_regime = _rule_based_regime(features, percentiles)

        # 4. HMM 시도
        model = load_model()
        if model is not None:
            # HMM 상태 → 레짐 매핑 (학습 순서 기반 휴리스틱)
            regime_map = {0: "A", 1: "C", 2: "B"}
            hmm_proba = _hmm_predict_proba(model, features, regime_map)
            if hmm_proba is not None:
                logger.info(
                    f"[레짐 모델] predict_proba (HMM): {date_str} → {hmm_proba}"
                )
                return hmm_proba

        # 5. 규칙 기반 확률 (해당=0.8, 나머지=0.1씩)
        proba = {"A": 0.1, "B": 0.1, "C": 0.1}
        proba[raw_regime] = 0.8

        logger.info(f"[레짐 모델] predict_proba (규칙 기반): {date_str} → {proba}")
        return proba

    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _has_valid_features(features: pd.Series) -> bool:
    """피처가 유효한지 확인 (NaN이 아닌 값이 하나 이상 있어야 함)."""
    if features is None or features.empty:
        return False
    return features.notna().any()


def _get_features_for_date(
    date_str: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[pd.Series]:
    """
    특정 날짜의 피처 Series 반환.

    우선 순위:
    1. feature.regime_features DB 조회 (이미 계산된 경우)
    2. regime.features.compute_features 직접 호출 (DB에 없는 경우)
    """
    # 1. feature.regime_features DB 직접 조회 (우선)
    db_features = _load_single_features_from_db(date_str, conn)
    if db_features is not None and _has_valid_features(db_features):
        return db_features

    # 2. features 모듈 직접 호출 시도
    try:
        from regime.features import compute_features
        features = compute_features(date_str, conn)
        if features is not None and _has_valid_features(features):
            return features
    except ImportError as e:
        logger.debug(f"[레짐 모델] features 모듈 임포트 실패: {e}")
    except Exception as e:
        logger.warning(f"[레짐 모델] compute_features 호출 실패: {e}")

    return None


def _load_single_features_from_db(
    date_str: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[pd.Series]:
    """feature.regime_features에서 단일 날짜 피처 조회."""
    try:
        row = conn.execute(
            """
            SELECT vix, vix3m, vxmt, vix_term, rv20, rv60,
                   ma200_gap, r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread
            FROM feature.regime_features
            WHERE date = CAST(? AS DATE)
            """,
            [date_str],
        ).fetchone()

        if row is None:
            logger.debug(f"[레짐 모델] feature.regime_features에 {date_str} 데이터 없음")
            return None

        cols = [
            "vix", "vix3m", "vxmt", "vix_term", "rv20", "rv60",
            "ma200_gap", "r12m", "r1m", "avg_corr20", "hy_spread", "ig_spread", "term_spread",
        ]
        return pd.Series(dict(zip(cols, row)))

    except Exception as e:
        logger.warning(f"[레짐 모델] regime_features DB 조회 실패: {e}")
        return None


def _load_features_from_db(
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[pd.DataFrame]:
    """feature.regime_features에서 기간 피처 조회."""
    try:
        df = conn.execute(
            """
            SELECT date, vix, vix3m, vxmt, vix_term, rv20, rv60,
                   ma200_gap, r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread
            FROM feature.regime_features
            WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            ORDER BY date
            """,
            [start, end],
        ).df()

        if df.empty:
            return None

        df = df.set_index("date")
        return df

    except Exception as e:
        logger.warning(f"[레짐 모델] regime_features 기간 조회 실패: {e}")
        return None


def _save_regime_label(
    date_str: str,
    regime: str,
    conn: duckdb.DuckDBPyConnection,
    shock_alarm: bool = False,
    raw_regime: Optional[str] = None,
) -> None:
    """
    feature.regime_labels에 레짐 레이블 저장.

    raw_regime: 히스테리시스 적용 전 규칙 기반 레짐 (상태 복원용).
    기존 데이터 DELETE 후 INSERT.
    """
    logger.info(f"[레짐 모델] regime_labels 저장 시작: {date_str}")
    try:
        pg_conn = get_pg_connection()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM feature.regime_labels WHERE date = %s::date",
            (date_str,),
        )
        cur.execute(
            """
            INSERT INTO feature.regime_labels (date, regime, shock_alarm, raw_regime, computed_at)
            VALUES (%s::date, %s, %s, %s, NOW())
            """,
            (date_str, regime, shock_alarm, raw_regime or regime),
        )
        pg_conn.commit()
        logger.info(f"[레짐 모델] regime_labels 저장 완료: {date_str} → regime={regime}, shock={shock_alarm}")
    except Exception as e:
        try:
            pg_conn.rollback()
        except Exception:
            pass
        logger.warning(f"[레짐 모델] PostgreSQL regime_labels 저장 실패: {date_str}, 오류={e}")
    finally:
        try:
            cur.close()
            pg_conn.close()
        except Exception:
            pass

    # 테스트 환경(인메모리 DuckDB)에서도 읽기가 가능하도록 DuckDB conn에도 best-effort 저장
    try:
        conn.execute(
            "DELETE FROM feature.regime_labels WHERE date = CAST(? AS DATE)",
            [date_str],
        )
        conn.execute(
            """
            INSERT INTO feature.regime_labels (date, regime, shock_alarm, raw_regime)
            VALUES (CAST(? AS DATE), ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET regime=EXCLUDED.regime,
                shock_alarm=EXCLUDED.shock_alarm, raw_regime=EXCLUDED.raw_regime
            """,
            [date_str, regime, shock_alarm, raw_regime or regime],
        )
    except Exception:
        pass  # 프로덕션 DuckDB(read-only postgres_scanner)에서는 무시
