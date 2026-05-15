# Quant Project Agent Guide

이 파일은 이 프로젝트를 다시 열었을 때 바로 작업 흐름을 복원하기 위한 운영 지침이다. 완료된 구현 이력과 오래된 계획은 `done.md`에 보관한다.

---

## 작업 방식 원칙

### 커뮤니케이션

- 각 단계(STEP) 시작 전: "이번 단계는 이렇게 진행하겠다" 알림
- 각 단계(STEP) 완료 후: "끝났다" 알림
- 진행 중 주요 항목마다 진행 상황 공유
- 코드 수정 전에는 어떤 파일을 왜 수정하는지 먼저 알림

### 권한

- 사용자가 모든 권한을 부여했으므로 파일 생성/수정/실행은 자율 진행
- 단, 데이터 삭제/초기화/DB drop 계열은 별도 명시 승인 전 금지

### 데이터 보호

- 기존 데이터 보호가 최우선이다.
- 금지 명령: `DROP DATABASE`, `DROP SCHEMA`, `TRUNCATE`, `docker rm`, `docker volume rm`, `git reset --hard`
- DB 작업 전에는 먼저 row count를 확인한다.
- Docker 컨테이너 `quant-pg`와 백업 파일 `data/quant_us.dump`는 검증 완료 전 삭제 금지.

---

## 에이전트/스킬 사용 원칙

사용자 요청을 받으면 먼저 관련 에이전트/스킬을 확인한다.

- 코드 리뷰: Python/보안/Go 등 관련 리뷰 에이전트 또는 스킬 사용
- 테스트 작성/리팩토링: TDD 또는 관련 테스트 스킬 우선
- 빌드 오류: build-error-resolver 계열 우선
- 새 기능: 기존 파일을 읽고 계획을 세운 뒤 구현

병렬 에이전트를 쓸 때는 다음을 지킨다.

- 모든 워커에게 공통 인터페이스 명세 전달
- "기존 파일 먼저 읽고 패턴 통일" 지시 포함
- 모호한 요구사항은 코드 패턴까지 구체화
- 오케스트레이터는 요약만 믿지 말고 실제 파일을 읽어 검증

---

## 로깅 및 실행 정책

모든 새 코드 또는 수정 코드에는 기존 로깅 스타일을 따른다.

- 함수 시작/종료: `logger.info()` 또는 `logger.debug()`
- 데이터 통계: 조회 로우 수, 종목 수, 처리 건수
- 필터링/검증: 단계별 통과/탈락 수
- 에러 상황: `logger.error()` 또는 `logger.warning()`
- 성능: 실행 시간은 `logger.debug()`

로그 포맷 예시:

```python
logger.info(f"[모듈명] 작업내용: 통계정보")
logger.info(f"[모멘텀 신호] 산출: 100개 종목, 범위=[-0.5, 0.8]")
logger.info(f"[DB 초기화] 완료: 5개 테이블 생성")
```

---

## 현재 운영 DB 기준 (2026-05-09)

Docker 없이 **로컬 Windows PostgreSQL**을 기본 운영 DB로 사용한다.

- PostgreSQL 설치 위치: `E:\PostgreSQL`
- PostgreSQL 데이터 디렉토리: `C:\Program Files\PostgreSQL\18\data`
- 사용 DB: `quant_us`
- 사용 포트: `5432`
- 프로젝트 `.env` 기준:

```env
PG_DSN=postgresql://postgres:rlacksdud1%21@127.0.0.1:5432/quant_us
```

비밀번호의 `!`는 URL에서 `%21`로 인코딩한다.

### 현재 로컬 DB 상태

2026-05-09 기준 로컬 PostgreSQL `quant_us`에 데이터가 이미 있음.

| 테이블                 | 상태                                           |
| ---------------------- | ---------------------------------------------- |
| `raw.prices`         | 779,599행, 2020-01-02 ~ 2026-04-02, 526개 티커 |
| `raw.fred_series`    | 16,420행                                       |
| `raw.sec_financials` | 17,243행                                       |

### Docker 백업

- Docker PostgreSQL 컨테이너 이름: `quant-pg` (`quant_pg` 아님)
- Docker DB 백업 파일: `data/quant_us.dump`
- 앞으로 기본 실행은 Docker가 아니라 로컬 PostgreSQL `127.0.0.1:5432/quant_us` 기준
- Docker 컨테이너/볼륨은 삭제하지 말고 보관

### 연결 확인

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_connection; c=get_connection(); print(c.execute('SELECT COUNT(*) FROM raw.prices').fetchone()); c.close()"
```

정상 출력 예시:

```text
(779599,)
```

---

## DB 아키텍처

DuckDB 단일 파일은 잠금 문제가 커서 운영 DB로 쓰지 않는다. 현재 기준은 **PostgreSQL 저장 + DuckDB in-memory 분석 읽기**다.

| 레이어           | 역할           | 연결 방식                                        | 플레이스홀더 |
| ---------------- | -------------- | ------------------------------------------------ | ------------ |
| PostgreSQL       | 쓰기/저장 OLTP | `get_pg_connection()` psycopg2                 | `%s`       |
| DuckDB in-memory | 읽기/분석 OLAP | `get_connection()` = DuckDB + postgres_scanner | `?`        |

연결 함수:

```python
get_pg_connection()      # 쓰기 전용, psycopg2 conn 반환
get_connection()         # 읽기 전용, DuckDB in-memory + postgres_scanner
get_duckdb_connection()  # get_connection() 내부 구현
init_db()                # PostgreSQL 스키마/테이블 생성
```

코딩 규칙:

- 쓰기 함수는 `get_pg_connection()` 사용, `%s` placeholder 사용
- 읽기 함수는 가능하면 `conn` 파라미터를 받고 DuckDB `?` placeholder 사용
- 테스트/분석 함수는 `conn` 주입이 실제로 동작해야 함
- 저장 함수는 테스트 호환을 위해 PG 저장 후 DuckDB `conn`에도 best-effort 저장하는 기존 패턴을 고려

---

## 프로젝트 전체 흐름

```text
데이터 수집
  ├─ 가격: yfinance 기반 S&P500 OHLCV 수집
  ├─ FRED: VIX, 금리, 크레딧, 경기 지표 수집
  └─ SEC: 10-K/10-Q 재무제표 수집
        ↓
PostgreSQL 저장
  ├─ raw.prices
  ├─ raw.fred_series
  ├─ raw.sec_financials
  ├─ raw.sp500_changes
  └─ raw.ticker_events
        ↓
DuckDB in-memory 읽기 레이어
  └─ postgres_scanner로 로컬 PostgreSQL을 read-only attach
        ↓
레짐 피처 산출
  ├─ VIX / VIX3M / VIX term
  ├─ SPY 실현변동성(rv20, rv60)
  ├─ SPY MA200 gap, 12M/1M 수익률
  ├─ 상위 거래대금 종목 평균 상관관계
  └─ HY/IG/term spread
        ↓
레짐 판단
  ├─ 규칙 기반 A/B/C 분류
  ├─ 히스테리시스 적용
  └─ shock_alarm 조건 확인
        ↓
전략 신호
  ├─ momentum: 12-1 모멘텀
  ├─ quality: ROE, 부채비율, EPS 변동성
  ├─ value: BM, EP, CFP
  └─ low_vol: 252일 저변동성
        ↓
포트폴리오 구성
  ├─ regime별 전략 가중치 결정
  ├─ 4개 전략 포트폴리오 결합
  ├─ risk-off 자산(SHY/TLT/CASH) 추가
  ├─ 종목/섹터 제약 적용
  ├─ VIX/손절 리스크 오버레이
  └─ drift 기반 리밸런싱 상태 저장
        ↓
운영/모니터링
  ├─ daily_run.py: 10단계 일일 파이프라인
  ├─ dashboard.py: Streamlit 4탭 대시보드
  ├─ pipeline_log 저장
  └─ Slack 알람(환경변수 없으면 스킵)
```

---

## 핵심 파일 역할

| 파일                                            | 역할                                                   |
| ----------------------------------------------- | ------------------------------------------------------ |
| `quant_us/db/init.py`                         | PostgreSQL 연결, DuckDB 읽기 연결, 스키마 생성         |
| `quant_us/data/collectors/price_collector.py` | yfinance 가격 수집, S&P500 구성/변경 이력 수집         |
| `quant_us/data/collectors/fred_collector.py`  | FRED 거시지표 수집                                     |
| `quant_us/data/collectors/sec_collector.py`   | SEC EDGAR 재무제표 수집, filed_date 기준 룩어헤드 방지 |
| `quant_us/regime/features.py`                 | 레짐 피처 산출 및 저장                                 |
| `quant_us/regime/model.py`                    | A/B/C 레짐 판단, 히스테리시스, HMM 보조                |
| `quant_us/regime/shock_alarm.py`              | VIX spike, backwardation, credit shock 등 급변 알람    |
| `quant_us/strategies/universe.py`             | S&P500 유니버스 및 거래대금/가격/상장폐지 필터         |
| `quant_us/strategies/momentum.py`             | 12-1 모멘텀 전략                                       |
| `quant_us/strategies/value.py`                | 밸류 전략                                              |
| `quant_us/strategies/quality.py`              | 퀄리티 전략                                            |
| `quant_us/strategies/low_vol.py`              | 저변동성 전략                                          |
| `quant_us/portfolio/weight_engine.py`         | 레짐별 전략 가중치, 결합 포트폴리오                    |
| `quant_us/portfolio/optimizer.py`             | 종목/섹터 제약, 리스크 오버레이, 스트레스 테스트       |
| `quant_us/portfolio/state.py`                 | drift 계산, 포트폴리오 상태 저장                       |
| `quant_us/backtest/engine.py`                 | 거래비용 포함 백테스트 엔진                            |
| `quant_us/backtest/walk_forward.py`           | Walk-Forward, DSR, PBO, 스트레스 검증                  |
| `quant_us/scripts/daily_run.py`               | 일일 운영 파이프라인 진입점                            |
| `quant_us/monitor/dashboard.py`               | Streamlit 대시보드                                     |

---

## daily_run.py 10단계

1. 주가 수집
2. SEC 수집: 현재 월 1회, 샘플 10종목만 수집하는 구조
3. FRED 수집
4. 데이터 품질 체크
5. 레짐 피처 산출
6. 레짐 판단 + shock alarm
7. 전략 신호 산출
8. 목표 포트폴리오 산출 + drift 계산 + 리밸런싱 판단 + 상태 저장
9. pipeline_log 저장
10. Slack 요약 알람

---

## 핵심 투자/검증 원칙

1. **서바이버십 편향 방지**: S&P500 과거 편출 종목 포함, 상장폐지/합병 이력 저장
2. **룩어헤드 방지**: SEC `filed_date` 기준으로만 재무 데이터 적용
3. **과적합 방지**: Walk-Forward, DSR, PBO로 검증
4. **히스테리시스**: 레짐 전환 시 2~3일 연속 조건 충족 필요
5. **VIX 중심 레짐**: 미국 시장 특화, VIX Term Structure 활용
6. **병렬 에이전트 패턴**: 공통 인터페이스 명세 + 상호 파일 참조 지시 필수

---

## 다음 수정 우선순위

1. **FRED 시리즈 ID 수정**

   - 현재 코드에 `VIXREM`, `VXMTSI`가 남아 있음.
   - `VIXREM`은 `VXVCLS`로 교체 필요.
   - `VXMTSI`는 FRED에 없으므로 제거 또는 별도 데이터 소스 필요.
   - 대상 파일: `fred_collector.py`, `regime/features.py`, 관련 테스트.
2. **테스트 가능성 개선**

   - `run_pipeline(date, conn=...)` 시그니처가 있으나 내부에서 `get_connection()`을 새로 호출하는 문제가 있었음.
   - 테스트용 in-memory DuckDB/모킹 conn이 실제로 주입되도록 정리 필요.
3. **운영 하드코딩 제거**

   - `PortfolioState(total_value=500)` 하드코딩 제거.
   - `optimize(..., top_n=10)` 운영 기준 명확화.
   - 대시보드의 총자본금 `$500` 하드코딩 제거 또는 UI 입력/환경변수화.
4. **SEC 일일 수집 범위 명확화**

   - `daily_run.py`는 월 1회에도 `universe[:10]` 샘플만 SEC 수집.
   - 전체 SEC 갱신은 별도 배치로 유지할지, 운영 파이프라인에 통합할지 결정 필요.
5. **밸류 전략 개선**

   - `value.py`는 market cap을 실제 시가총액이 아니라 가격 프록시로 사용 중.
   - shares outstanding 또는 외부 market cap 데이터 보강 전까지 밸류 팩터 신뢰도 제한.
6. **유니버스/서바이버십 검증**

   - `raw.sp500_changes` 기반 과거 구성종목 복원이 충분한지 검증 필요.
   - 과거 편출/합병/티커변경 데이터 보강 필요.
7. **문서/비밀정보 정리**

   - 문서에 API 키/비밀번호가 남아 있으면 정리 필요.
   - 현재 로컬 개인 운영을 위해 `AGENTS.md`에는 PG_DSN 기준을 기록해 둠.

---

## 실행 명령

프로젝트 루트:

```powershell
cd "E:\personal project\quant"
```

일일 파이프라인:

```powershell
python quant_us/scripts/daily_run.py
python quant_us/scripts/daily_run.py --date 2026-04-02
python quant_us/scripts/daily_run.py --date 2026-04-02 --dry-run
```

대시보드:

```powershell
streamlit run quant_us/monitor/dashboard.py
```

테스트:

```powershell
python -m pytest tests/ -v
```

---

## Notion 작업 규칙

- 노션 관련 작업 요청 시 항상 MCP를 통해 직접 접근한다.
- 검색 시 notion-search 도구를 먼저 사용한다.
- 해당하는 페이지가 있으면 그 페이지에 작성한다.
- 해당하는 페이지가 없다면 생성 후 진행한다.
- notion-create-pages에는 항상 순수 UUID를 사용한다.
  - `data_source_id`: UUID만 사용
  - `page_id`: UUID 가능
  - `database_id`: UUID 가능
  - `collection://`, `page://` 프리픽스 포함 금지


다 한 계획은 done.md에 적기
