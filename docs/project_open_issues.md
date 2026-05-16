# 프로젝트 미완성 이슈 추적

작성일: 2026-05-17

이 문서는 2026-05-17 기준 전체 프로젝트 점검에서 확인한 남은 문제를 한곳에 모은 인덱스다. 세부 구현 계획은 기존 `docs/*.md` 문서를 우선 참고한다.

---

## 현재 상태 요약

- 전체 테스트: `230 passed, 2 failed`
- 작업트리: 점검 당시 변경 없음
- FRED 시리즈 ID 수정은 코드 기준 완료
- DB 아키텍처 목표는 PostgreSQL 단일화로 변경
- 남은 핵심 리스크는 PostgreSQL 단일화, 테스트 격리, 운영 하드코딩, 데이터 신뢰도, 문서 정합성이다.

---

## P0. PostgreSQL 단일화

### 문제

현재 코드는 PostgreSQL 저장과 DuckDB in-memory 읽기 레이어를 혼용한다. 프로젝트 규모상 PostgreSQL만으로 충분하며, DuckDB 혼용은 연결 함수, placeholder, 테스트 fixture, `.df()` 호출 복잡도를 키운다.

### 근거 위치

- `AGENTS.md`
- `README.md`
- `quant_us/db/init.py`
- DuckDB 전용 `?` placeholder와 `.df()` 호출이 여러 모듈에 남아 있음

### 권장 조치

1. 신규 코드는 PostgreSQL only 기준으로 작성한다.
2. `get_connection()`/`get_pg_connection()` 역할을 PostgreSQL 기준으로 정리한다.
3. `?` placeholder를 `%s` 기준으로 전환한다.
4. DuckDB `.df()` 호출을 pandas/cursor 기반 변환으로 바꾼다.
5. 테스트는 별도 PostgreSQL 테스트 DB 또는 mock/fixture로 전환한다.

관련 문서: `docs/improve_test_connection_injection.md`

실행 계획: `docs/postgres_only_migration_plan.md`

---

## P0. 테스트와 로컬 PostgreSQL 의존성 분리

### 문제

`tests/test_portfolio_state.py`의 일부 테스트가 주입된 fixture를 쓰다가 마지막 검증에서 실제 PostgreSQL에 직접 접속한다. 로컬 DB 연결이 실패하면 단위 테스트 전체가 실패한다.

### 확인된 증상

- `python -m pytest tests/ -q`
- 결과: `230 passed, 2 failed`
- 실패 테스트:
  - `TestPortfolioStatePersistence.test_save_and_retrieve_state`
  - `TestIntegration.test_full_workflow`
- 실패 원인:
  - `get_pg_connection()` 호출 중 `psycopg2` 연결 단계에서 `UnicodeDecodeError`

### 근거 위치

- `tests/test_portfolio_state.py:202`
- `tests/test_portfolio_state.py:338`
- `quant_us/db/init.py:27`

### 권장 조치

1. 실제 PostgreSQL 검증 테스트는 `integration` 마커로 분리한다.
2. 기본 단위 테스트는 운영 DB가 아니라 별도 테스트 DB 또는 mock/fixture만 검증하도록 바꾼다.
3. CI/일반 로컬 테스트 명령은 DB 없이 통과해야 한다.
4. 실제 운영 DB 검증 명령은 별도 문서화한다.

관련 문서: `docs/improve_test_connection_injection.md`

---

## P0. 운영 하드코딩 제거

### 문제

운영 결과에 직접 영향을 주는 값이 코드에 고정되어 있다. 페이퍼 트레이딩 또는 실계좌 규모와 다르면 drift, 보유 수량, 리밸런싱 판단이 왜곡된다.

### 확인된 값

- `PortfolioState(total_value=500)`
- `optimize(..., top_n=10)`
- 대시보드 총자본금 `$500`

### 근거 위치

- `quant_us/scripts/daily_run.py:498`
- `quant_us/scripts/daily_run.py:521`
- `quant_us/monitor/dashboard.py:615`

### 권장 조치

1. `PORTFOLIO_TOTAL_VALUE` 환경변수를 추가한다.
2. `PORTFOLIO_TOP_N` 또는 별도 운영 설정을 추가한다.
3. `.env.example`, `README.md`에 설정값을 문서화한다.
4. 대시보드는 설정값을 기본값으로 쓰되 UI 입력으로 조정 가능하게 한다.

관련 문서: `docs/remove_operational_hardcoding.md`

---

## P1. 밸류 전략 market cap 프록시 제거

### 문제

밸류 전략의 BM, EP, CFP 계산에서 실제 시가총액 대신 `adj_close`를 market cap 프록시로 사용한다. 이 상태에서는 밸류 팩터의 경제적 의미가 약하고 백테스트 해석이 왜곡될 수 있다.

### 근거 위치

- `quant_us/strategies/value.py:115`
- `quant_us/strategies/value.py:148`

### 권장 조치

1. `raw.prices.market_cap` 채움 상태를 확인한다.
2. 실제 market cap 데이터 소스를 결정한다.
3. 데이터가 없으면 가격 프록시를 쓰지 말고 해당 종목 제외 또는 value 전략 비활성화 정책을 정한다.
4. 룩어헤드 방지를 위해 기준일 이후에 알게 된 shares/market cap을 과거 계산에 쓰지 않는다.

관련 문서: `docs/improve_value_strategy_market_cap.md`

---

## P1. SEC 일일 수집 범위 명확화

### 문제

일일 파이프라인의 SEC 단계는 월 1일에만 실행되고, 실행 시에도 `universe[:10]` 샘플만 수집한다. 로그에는 샘플 10종목이라고 표시하지만 코드 주석에는 전체 유니버스 증분 수집처럼 읽히는 부분이 있다.

### 근거 위치

- `quant_us/scripts/daily_run.py:274`
- `quant_us/scripts/daily_run.py:283`
- `quant_us/scripts/daily_run.py:290`

### 권장 조치

1. daily SEC 단계의 역할을 샘플/헬스체크로 명확히 표현한다.
2. 전체 SEC 갱신은 `scripts/data_collection/collect_sec_all.py` 같은 별도 배치로 문서화한다.
3. 필요하면 `--skip-sec`, `--sec-sample-size`, `--sec-full` 옵션을 추가한다.
4. 전체 수집은 rate limit과 예상 소요 시간을 문서화한다.

관련 문서: `docs/clarify_sec_collection_scope.md`

---

## P1. 섹터 메타데이터 미구현

### 문제

전략과 포트폴리오 제약에서 섹터 정보가 필요하지만, 현재 일부 코드는 고정 매핑 또는 미구현 상태다. 섹터 제약, 섹터 중립, 금융주 필터 등의 신뢰도가 제한된다.

### 근거 위치

- `quant_us/strategies/low_vol.py:84`
- `quant_us/strategies/low_vol.py:99`
- `quant_us/strategies/quality.py:69`
- `quant_us/strategies/quality.py:74`
- `quant_us/strategies/momentum.py:236`

### 권장 조치

1. `normalized.ticker_info` 또는 별도 master 테이블을 추가한다.
2. 최소 필드: `ticker`, `sector`, `industry`, `source`, `as_of_date`, `updated_at`
3. 저변동성, 퀄리티, 포트폴리오 최적화가 같은 섹터 소스를 보게 한다.
4. 섹터 데이터가 없을 때의 폴백 정책을 명확히 한다.

---

## P1. 문서와 비밀정보 정리

### 문제

문서에 실제처럼 보이는 API 키와 과거 Docker PostgreSQL 기준이 남아 있거나 보관 이력으로 섞여 있다. 현재 운영 기준은 로컬 Windows PostgreSQL `127.0.0.1:5432/quant_us`이며, 신규 문서는 PostgreSQL 단일화 기준으로 작성해야 한다.

### 확인된 항목

- `DATA_COLLECTION_GUIDE.md`에 FRED API key 값 노출
- `README.md`는 현재 기준으로 갱신했으며, 과거 Docker `5433` 기준은 보관 이력으로만 취급
- `done.md`에 과거 Docker 운영 기준 보관
- `quant_us/db/init.py` 기본 `PG_DSN`도 과거 Docker `localhost:5433` 기준

### 근거 위치

- `DATA_COLLECTION_GUIDE.md:16`
- `README.md:62`
- `README.md:68`
- `README.md:237`
- `done.md:197`
- `done.md:204`
- `quant_us/db/init.py:24`

### 권장 조치

1. 문서의 API 키는 placeholder로 교체한다.
2. README는 현재 운영 기준인 로컬 PostgreSQL `5432` 기준으로 갱신한다.
3. 과거 Docker 기준은 `done.md`에만 보관하고 현재 가이드와 분리한다.
4. `quant_us/db/init.py`의 기본 DSN을 현재 기준으로 바꾸거나, 기본값 없이 환경변수 필수로 전환한다.

관련 문서: `docs/clean_documentation_secrets.md`

---

## P2. FRED 시리즈 ID 잔여 검증

### 현재 상태

코드 기준으로는 완료되어 있다.

- `VIXREM`은 `VXVCLS`로 교체됨
- `VXMTSI`는 제거됨
- FRED 수집기는 12개 시리즈 기준

### 남은 확인

1. 기존 DB `raw.fred_series`에 불필요한 `VIXREM`/`VXMTSI` 데이터가 있는지 읽기 쿼리로 확인한다.
2. DB 삭제는 별도 승인 전 하지 않는다.
3. `AGENTS.md`의 다음 우선순위 목록에서 완료 항목으로 이동한다.

관련 문서: `docs/fix_fred_series_ids.md`

---

## P2. 서바이버십 편향 검증

### 문제

`raw.sp500_changes`와 `raw.ticker_events` 기반 과거 구성종목 복원이 충분히 정확한지 아직 검증이 필요하다. 백테스트 성과가 투자 판단에 쓰이려면 이 검증이 중요하다.

### 근거 위치

- `quant_us/strategies/universe.py`
- `docs/validate_survivorship_universe.md`

### 권장 조치

1. `raw.sp500_changes` 행 수, 날짜 범위, action 분포를 확인한다.
2. `raw.ticker_events`에 상장폐지/합병/티커변경 이벤트가 충분한지 확인한다.
3. 과거 대표 날짜별 유니버스 크기를 검증한다.
4. 현재 S&P500 목록만으로 과거를 재구성하는 fallback이 과도하게 작동하지 않는지 확인한다.

관련 문서: `docs/validate_survivorship_universe.md`

---

## 권장 실행 순서

1. P0 테스트 분리
2. P0 운영 하드코딩 제거
3. P1 문서와 비밀정보 정리
4. P1 SEC 수집 범위 명확화
5. P1 밸류 전략 market cap 개선
6. P1 섹터 메타데이터 추가
7. P2 FRED DB 잔여 검증
8. P2 서바이버십 편향 검증
