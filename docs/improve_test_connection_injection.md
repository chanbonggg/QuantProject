# PostgreSQL 단일화와 테스트 연결 개선 계획

## 목표

DuckDB 테스트 fixture 의존을 줄이고, PostgreSQL 단일 연결 구조에서도 테스트가 안정적으로 동작하도록 정리한다.

## 배경

현재 일부 함수는 DuckDB 전용 연결과 PostgreSQL 연결을 혼용한다. 이 때문에 `?` placeholder, `.df()` 호출, psycopg2 `%s` placeholder가 섞여 테스트와 운영 경로가 달라진다. 앞으로는 PostgreSQL only 구조로 정리한다.

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/portfolio/optimizer.py`
- `quant_us/regime/features.py`
- `quant_us/regime/model.py`
- `quant_us/portfolio/state.py`
- `quant_us/db/init.py`

## 구현 단계

1. `get_connection()`과 `get_pg_connection()`의 역할을 PostgreSQL 기준으로 재정의한다.
2. DuckDB 전용 `.df()` 호출을 `pandas.read_sql_query()` 또는 cursor fetch 변환으로 바꾼다.
3. 쿼리 placeholder를 `%s` 기준으로 통일한다.
4. `run_pipeline(..., conn=None)`에서 전달받은 PostgreSQL 연결이 내부 단계까지 유지되도록 한다.
5. 단위 테스트는 mock/fixture 또는 별도 PostgreSQL 테스트 DB를 사용한다.
6. 실제 운영 DB가 필요한 검증은 `integration` 테스트로 분리한다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 PostgreSQL 단일화 구조에 맞게 갱신한다.
- 로컬 운영 DB 없이도 가능한 단위 테스트와 실제 DB가 필요한 통합 검증을 구분한다.

## 주의사항

- 운영 DB `quant_us`에 테스트 데이터를 직접 쓰지 않는다.
- 가능하면 별도 `quant_us_test` 또는 트랜잭션 롤백 fixture를 사용한다.
- DuckDB 의존 제거는 기능별로 나눠 진행한다.
