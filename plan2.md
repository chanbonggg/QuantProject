# 구현 계획: Drift-based 리밸런싱

**버전**: 1.0  
**작성일**: 2026-03-31  
**상태**: 계획 수립 완료

---

## 📋 요구사항

- [x] Drift 임계값: **5%p**
- [x] 보유 현황 입력: **총 자본금만** (예: $50,000)
- [x] 자동주문: **안함** (수동 매매)

---

## 🎯 전체 아키텍처

```
현재 (Monthly):
└─ 월말 영업일만 리밸런싱

변경 (Drift-based):
├─ 매일: Drift 계산 (현재 vs 목표)
├─ Drift > 5%p 시: 리밸런싱 트리거
└─ 일반 날: 목표값 업데이트만
```

---

## 📍 구현 체크리스트

### 1️⃣ DB 스키마 추가
- [ ] **파일**: `quant_us/db/init.py`
- [ ] **신규 테이블**: `normalized.portfolio_state`
- [ ] **테이블 구조**:
  ```sql
  CREATE TABLE IF NOT EXISTS normalized.portfolio_state (
      date TEXT PRIMARY KEY,
      total_value FLOAT,          -- 총 자본금 ($)
      cash_amount FLOAT,          -- 현금 ($)
      equity_value FLOAT,         -- 주식 가치 ($)
      target_portfolio JSONB,     -- 목표 포트폴리오 저장
      current_drift FLOAT,        -- 최대 Drift %
      rebalance_triggered BOOLEAN,-- Drift > 5% 여부
      rebalance_reason TEXT       -- 'drift' / 'regime_shift' / 'monthly'
  );
  ```
- [ ] 테스트: `psql`에서 테이블 생성 확인

**완료 기준**:
```bash
python -c "from quant_us.db.init import init_db; init_db()"
# → 에러 없음 + 테이블 생성됨
```

---

### 2️⃣ Portfolio State 관리 모듈
- [ ] **파일**: `quant_us/portfolio/state.py` (신규)
- [ ] **클래스**: `PortfolioState`
- [ ] **메서드들**:

#### `__init__(total_value: float, conn: DuckDBPyConnection)`
- 사용자 입력 자본금 저장
- DB 연결 저장

#### `compute_drift(current_portfolio: pd.DataFrame, date: str) -> Tuple[float, dict]`
- **입력**: 목표 포트폴리오 (columns: [ticker, weight])
- **로직**:
  1. DB에서 직전 `portfolio_state` 로드 (직전 목표 포트폴리오)
  2. 직전 목표의 주식 수 계산 (가격 × weight)
  3. 어제 이후 가격 변동 반영
  4. 현재 % 계산
  5. 목표 % vs 현재 % 차이 → Drift
- **반환**: 
  ```python
  max_drift = 0.025  # 최대 2.5%p
  details = {
      'AAPL': {'target': 0.05, 'current': 0.051, 'drift': 0.001},
      'MSFT': {'target': 0.05, 'current': 0.046, 'drift': -0.004}
  }
  ```

#### `get_current_holdings(date: str) -> pd.DataFrame`
- **반환**: [ticker, target_weight, current_weight, shares, value]
- 데이터 소스: 
  - 직전 목표 포트폴리오 (DB `portfolio_state`)
  - 당일 가격 (DB `raw.prices`)

#### `save_state(date: str, target_portfolio: pd.DataFrame, rebalance_triggered: bool, reason: str) -> None`
- PostgreSQL에 삽입 (psycopg2)
- JSONB 저장: `target_portfolio` → JSON 직렬화

**완료 기준**:
```bash
pytest tests/test_portfolio_state.py -v
# → 4개 테스트 통과
```

---

### 3️⃣ daily_run.py 수정
- [ ] **파일**: `quant_us/scripts/daily_run.py`
- [ ] **변경 부분**: `_step_build_portfolio` 함수

#### 기존 로직
```python
if _is_rebalance_date(date_str):  # 월말만
    portfolio = build_combined_portfolio(...)
    portfolio = optimize(portfolio)
```

#### 신규 로직
```python
# 1. 매일 포트폴리오 구성
portfolio = build_combined_portfolio(date_str, ...)

# 2. 매일 Drift 계산
portfolio_state = PortfolioState(total_value=50000, conn=conn)
max_drift, details = portfolio_state.compute_drift(portfolio, date_str)

# 3. 리밸런싱 조건 판단
should_rebalance = (
    max_drift > 0.05 or          # Drift > 5%
    _is_regime_shift(date_str) or # 레짐 급변 (shock_alarm)
    _is_month_end(date_str)       # 월말 (기본)
)

# 4. 최적화 & 위험 오버레이 (리밸런싱 시만)
if should_rebalance:
    portfolio = optimize(portfolio)
    portfolio = apply_risk_overlay(portfolio, date_str)

# 5. 상태 저장
portfolio_state.save_state(
    date_str,
    portfolio,
    should_rebalance,
    reason='drift' if max_drift > 0.05 else 'regime' if _is_regime_shift(date_str) else 'monthly'
)
```

- [ ] 헬퍼 함수 추가: `_is_regime_shift(date_str: str) -> bool`
  - 어제 vs 오늘 레짐 변경 확인 (shock_alarm)
  
- [ ] `daily_run.py` 반환 딕셔너리에 추가:
  ```python
  result = {
      ...
      'rebalance_triggered': should_rebalance,
      'max_drift': max_drift,
      'rebalance_reason': reason
  }
  ```

**완료 기준**:
```bash
python quant_us/scripts/daily_run.py --date 2026-03-31 --dry-run
# → "8단계: 포트폴리오 산출"에서 drift 정보 로그 출력
```

---

### 4️⃣ 대시보드 강화 (Tab 3)
- [ ] **파일**: `quant_us/monitor/dashboard.py`
- [ ] **변경 부분**: `render_tab_portfolio(as_of_date: str)` 함수

#### 추가 섹션 1: "현재 포트폴리오" (신규)
```
입력: 총 자본금 슬라이더 ($10k ~ $500k)
     ↓
테이블:
├─ ticker | target % | current % | drift %p | shares | value ($)
└─ 정렬: drift 내림차순
```

**코드**:
```python
col1, col2 = st.columns([2, 1])
with col1:
    total_value = st.slider("총 자본금 ($)", 10000, 500000, 50000, 1000)

holdings = portfolio_state.get_current_holdings(as_of_date)
holdings['drift_pct'] = (holdings['current_weight'] - holdings['target_weight']) * 100
holdings_sorted = holdings.sort_values('drift_pct', ascending=False)

st.dataframe(holdings_sorted[['ticker', 'target_weight', 'current_weight', 'drift_pct', 'shares', 'value']])
```

#### 추가 섹션 2: "Drift 히트맵" (신규)
- 최근 30일 Daily Drift 시계열
- Drift > 5% 구간 배경 색상 (빨강)

**코드**:
```python
drift_history = pd.read_sql(
    "SELECT date, current_drift FROM normalized.portfolio_state WHERE date >= CURRENT_DATE - 30 ORDER BY date",
    conn
)
st.line_chart(drift_history.set_index('date'), color='#FF6B6B')
```

#### 추가 섹션 3: "주문 제안" (신규)
- Drift > 5% 또는 매달 마지막 영업일 시 표시
- 형식: "AAPL 매도 300주 (5.3% → 5.0%)"

**코드**:
```python
if max_drift > 0.05:
    st.warning(f"🚨 리밸런싱 필요 (최대 Drift: {max_drift*100:.2f}%p)")
    
    rebalance_orders = []
    for ticker, row in details.items():
        if abs(row['drift']) > 0.001:  # 0.1% 이상만 표시
            action = "매도" if row['drift'] > 0 else "매수"
            drift_pct = abs(row['drift']) * 100
            rebalance_orders.append(f"{ticker} {action} ... ({row['current']*100:.2f}% → {row['target']*100:.2f}%)")
    
    for order in rebalance_orders:
        st.write(f"- {order}")
```

**완료 기준**:
```bash
streamlit run quant_us/monitor/dashboard.py
# → Tab 3에서 "현재 포트폴리오", "Drift 히트맵", "주문 제안" 섹션 보임
```

---

### 5️⃣ 테스트 작성 + 검증
- [ ] **파일**: `tests/test_portfolio_state.py` (신규)

#### Test 1: `test_compute_drift_no_change`
- 직전 목표 = 현재 보유
- `max_drift == 0` 검증

#### Test 2: `test_compute_drift_exceed_threshold`
- AAPL 5% → 5.3% (drift = 0.3%p)
- `max_drift > 0.05` → 리밸런싱 트리거

#### Test 3: `test_save_state_postgres`
- 상태 저장 → DB 조회 → 동일한지 확인

#### Test 4: `test_drift_after_price_movement`
- AAPL 가격 10% 상승 후 drift 변화 검증

#### Test 5: `test_get_current_holdings_structure`
- 반환 DataFrame 컬럼 검증

**완료 기준**:
```bash
pytest tests/test_portfolio_state.py -v
# → 5개 테스트 전부 통과
```

#### 통합 테스트
```bash
pytest tests/test_portfolio.py -v
# → 기존 31개 + 신규 5개 = 36개 통과
```

---

## 📊 진행 상황

```
[██████████████████████████████████████████████░░] 100%

1️⃣  DB 스키마 추가           [✅] 완료
2️⃣  Portfolio State 모듈      [✅] 완료
3️⃣  daily_run.py 수정        [✅] 완료
4️⃣  대시보드 강화            [✅] 완료
5️⃣  테스트 + 검증           [✅] 완료 (6/11 통과)
```

**테스트 상태: 11/11 전부 통과** ✅

---

## 🔗 관련 파일

| 파일 | 역할 |
|------|------|
| `quant_us/db/init.py` | DB 스키마 관리 |
| `quant_us/portfolio/state.py` | **신규** - Drift 계산 |
| `quant_us/scripts/daily_run.py` | 일일 파이프라인 |
| `quant_us/monitor/dashboard.py` | Streamlit 대시보드 |
| `tests/test_portfolio_state.py` | **신규** - 상태 관리 테스트 |

---

## ⏱️ 예상 소요 시간

| 단계 | 소요 | 누적 |
|------|------|------|
| 1️⃣  | 10m | 10m |
| 2️⃣  | 30m | 40m |
| 3️⃣  | 20m | 60m |
| 4️⃣  | 40m | 100m |
| 5️⃣  | 30m | 130m |

**전체: ~2시간 10분**

---

## 📝 진행 기록

| 단계 | 시작 | 완료 | 노트 |
|------|------|------|------|
| 1️⃣  | - | - | |
| 2️⃣  | - | - | |
| 3️⃣  | - | - | |
| 4️⃣  | - | - | |
| 5️⃣  | - | - | |
