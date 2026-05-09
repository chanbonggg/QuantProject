# 테스트 연결 주입 개선 계획

## 목표

`run_pipeline(date, conn=...)`처럼 외부에서 전달한 테스트/분석 연결이 실제 내부 단계에서도 사용되도록 정리한다.

## 배경

현재 일부 함수는 `conn` 파라미터를 받지만 내부에서 다시 `get_connection()`을 호출한다. 이 때문에 테스트용 in-memory DuckDB를 넘겨도 로컬 PostgreSQL에 붙으려 하며, 테스트가 환경 의존적으로 실패한다.

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/portfolio/optimizer.py`
- `quant_us/regime/features.py`
- `quant_us/regime/model.py`
- `quant_us/portfolio/state.py`

## 구현 단계

1. `run_pipeline(..., conn=None)`에서 전달받은 `conn`을 `_run_single_date()`로 넘긴다.
2. `_run_single_date(date_str, dry_run, conn=None)`가 외부 conn이 있으면 새 연결을 만들지 않도록 한다.
3. 내부 단계 함수는 이미 받은 `conn`을 계속 전달한다.
4. 쓰기 작업은 PostgreSQL 연결이 필요한지, 테스트용 DuckDB best-effort 저장으로 충분한지 구분한다.
5. `compute_portfolio_stats()`처럼 conn이 선택인 함수도 테스트에서 외부 conn을 넘기도록 테스트를 조정한다.
6. 환경 DB 없이 돌아가야 하는 단위 테스트와 실제 PostgreSQL이 필요한 통합 테스트를 구분한다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 현재 연결 주입 구조에 맞게 갱신한다.
- 로컬 PostgreSQL 없이도 가능한 단위 테스트와 실제 DB가 필요한 통합 검증을 구분한다.

## 주의사항

- 운영 코드에서 PostgreSQL 쓰기를 무리하게 DuckDB 쓰기로 바꾸지 않는다.
- 테스트 격리를 위해 “읽기 conn 주입”과 “쓰기 저장소” 책임을 명확히 나눈다.
