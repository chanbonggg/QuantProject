# Plan: daily_run 테스트 오류 3건 수정

## Context

2026-04-03 daily_run.py 실행 테스트에서 3가지 오류 발견:
1. **FRED 시리즈 ID 오류**: VIXREM, VXMTSI가 FRED에 존재하지 않음 → 매 실행마다 실패 로그
2. **날짜 계산 오류**: `datetime.today()` = 로컬 시간(KST) 사용 → 미국 미개장 날짜 수집 시도
3. **가격 수집 거짓 양성**: `collect_daily()` 가 빈 데이터에도 "성공" 보고 (영향 낮음, 선택 수정)

## 수정 대상 파일

| 파일 | 변경 내용 |
|------|----------|
| `quant_us/data/collectors/fred_collector.py` | VIXREM→VXVCLS 교체, VXMTSI 제거 (13→12개) |
| `quant_us/regime/features.py` | FRED_SERIES_MAP의 `vix3m` 매핑: VIXREM→VXVCLS |
| `quant_us/scripts/daily_run.py` | `datetime.today()` → US Eastern 시간 사용 |
| `tests/test_regime.py` | VIXREM → VXVCLS 테스트 픽스처 수정 |

---

## STEP 1: FRED 시리즈 ID 수정 (VIXREM → VXVCLS, VXMTSI 제거)

### 근본 원인

시리즈 ID가 처음부터 잘못 등록됨:
- `VIXREM` → **실제 존재하지 않는 ID**. 올바른 시리즈: **`VXVCLS`** (CBOE S&P 500 3-Month Volatility Index, 2007~현재)
- `VXMTSI` → **FRED에 6개월 VIX 시리즈 자체가 없음**. CBOE에서 VIX6M을 발행하지만 FRED에는 미등록.

### 1-1. `fred_collector.py` (라인 33-51)

**변경**: VIXREM → VXVCLS 교체, VXMTSI 제거 (13→12개)

```python
# BEFORE
FRED_SERIES: list[str] = [
    ...
    "VIXCLS",        # VIX
    "VIXREM",        # VIX 3-Month Forward         ← 잘못된 ID
    "VXMTSI",        # VXMT (VIX 20-Year Forward)  ← FRED에 없음
]

# AFTER
FRED_SERIES: list[str] = [
    ...
    "VIXCLS",        # VIX (30일)
    "VXVCLS",        # VIX3M (3개월) — CBOE S&P 500 3-Month Volatility Index
]
```

**파일 상단 docstring 수정**: "수집 시리즈 (13개)" → "수집 시리즈 (12개)", "VIXREM" → "VXVCLS", VXMTSI 제거

### 1-2. `regime/features.py` — FRED_SERIES_MAP 수정 (라인 51-57)

**변경**: `vix3m` 매핑을 올바른 시리즈 ID로 교체

```python
# BEFORE
FRED_SERIES_MAP = {
    "vix": "VIXCLS",
    "vix3m": "VIXREM",      # ← 잘못된 ID
    ...
}

# AFTER
FRED_SERIES_MAP = {
    "vix": "VIXCLS",
    "vix3m": "VXVCLS",      # VIX3M (CBOE 3-Month Volatility Index)
    ...
}
```

### 1-3. 하류 영향 분석 (변경 불필요)

**vix3m과 vix_term이 정상 데이터를 받게 되므로, 기존 코드 전부 그대로 동작:**

| 모듈 | 사용 | 변경 필요 |
|------|------|----------|
| `regime/features.py:369-374` | `vix_term = vix3m / vix` | 불필요 (vix3m이 이제 실제 값) |
| `regime/model.py:269` | `b_vix_term = vix_term < 1.0` | 불필요 (실제 값으로 판단) |
| `regime/shock_alarm.py:242-247` | VIX Backwardation 감지 | 불필요 (실제 값으로 판단) |
| `monitor/dashboard.py:519-524` | VIX Term Structure 차트 | 불필요 (데이터 있으면 표시) |

**핵심 개선**: vix_term이 항상 None이던 문제가 해결 → 레짐 B 판단에 VIX term structure 조건이 실제로 작동하게 됨

### 1-4. `tests/test_regime.py` — 테스트 픽스처 수정

테스트에서 `VIXREM` mock 데이터를 `VXVCLS`로 변경:
- 라인 140: `"VIXREM": 21.0` → `"VXVCLS": 21.0`
- 라인 281: INSERT 문의 series_id `VIXREM` → `VXVCLS`

### 1-5. 기존 DB 데이터 처리

VIXREM으로 저장된 기존 raw.fred_series 데이터는 쓸모없음 (잘못된 ID로 수집 자체가 실패했으므로 데이터 0건).
VXVCLS로 새로 수집하면 2007년부터 데이터가 채워짐.

---

## STEP 2: 날짜 계산을 US Eastern으로 변경

### 2-1. `daily_run.py` 라인 732

**문제**: `datetime.today()` = 시스템 로컬 시간(KST, UTC+9)
- KST 04/04 오전 = US Eastern 04/03 오후 → 미국은 아직 04/03 장 진행 중
- `bdate_range(..., end=today)` 에 04/03 포함 → 미완성 데이터 수집 시도

**변경**:

```python
# BEFORE
today = datetime.today().strftime("%Y-%m-%d")

# AFTER
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
```

**참고**: Python 3.9+ 내장 `zoneinfo` 사용 (pytz 불필요)

### 2-2. import 추가

`daily_run.py` 상단 import 섹션에 `from zoneinfo import ZoneInfo` 추가.

---

## STEP 3 (선택): 가격 수집 거짓 양성 개선

**현재 영향**: 운영 중 미래 날짜 호출은 STEP 2 수정으로 사라짐.
**결론**: STEP 2 수정으로 근본 원인 제거됨. 추가 수정 불필요.

---

## 수정하지 않는 것들

- DB 스키마 (`feature.regime_features` 테이블): vix3m, vxmt, vix_term 컬럼 유지
- `FEATURE_COLUMNS` 리스트: 유지 (DB 컬럼 순서와 일치해야 함)
- `collect_daily()` 반환 타입: bool → int 변경은 별도 작업 (이번 범위 아님)
- vix_term 계산 로직: 변경 없음 (VXVCLS로 교체하면 자연 정상화)
- regime/model.py, shock_alarm.py, dashboard.py: 변경 없음 (vix3m/vix_term 데이터가 채워지면 기존 코드 정상 동작)

---

## 검증

```bash
# 1. 테스트 실행
python -m pytest tests/test_regime.py -v
python -m pytest tests/test_daily_run.py -v
python -m pytest tests/ -v

# 2. FRED 수집 확인 (12개 전체 성공 예상)
python -c "from quant_us.data.collectors.fred_collector import collect_all; collect_all()"

# 3. dry-run으로 날짜 확인 (US Eastern 기준)
python quant_us/scripts/daily_run.py --dry-run

# 4. 실제 파이프라인 실행 (최신 영업일)
python quant_us/scripts/daily_run.py
```
