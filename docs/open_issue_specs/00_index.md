# 미완성 이슈 기능별 명세 인덱스

작성일: 2026-06-04

기준 문서: `docs/project_open_issues.md`

---

## 확인 결과

### 아직 구현되지 않은 항목

| 기능 | 상태 근거 | 명세 |
| --- | --- | --- |
| PostgreSQL 단일화 / 테스트 격리 | `pytest.ini`, `tests/conftest.py`, `quant_us/db/query.py` 없음. 운영 코드와 테스트에 DuckDB import, `.df()` 호출, `get_duckdb_connection()` 남음 | `01_postgres_only_and_tests.md` |
| 운영 설정화 | `daily_run.py`에 `PortfolioState(total_value=500)`, `optimize(..., top_n=10)` 남음. `.env.example` 없음 | `02_operational_config.md` |
| 문서와 비밀정보 정리 | 문서에 과거 Docker/DuckDB 기준과 FRED 잔여 문구가 섞여 있음. `.env.example` 없음 | `03_documentation_secrets.md` |
| SEC 수집 범위 명확화 | `--skip-sec`, `--sec-sample-size`, `--sec-full` 옵션 없음. daily SEC 주석이 전체 수집처럼 읽힘 | `04_sec_collection_scope.md` |
| 밸류 전략 market cap 개선 | `value.py`에서 `adj_close`를 market cap 프록시로 사용 | `05_value_market_cap.md` |
| 섹터 메타데이터 | `low_vol.py` 고정 매핑, `quality.py` TODO, `momentum.py` sector neutral 미구현 | `06_sector_metadata.md` |
| 서바이버십 편향 검증 | DB 진단/대표 날짜 검증/fixture 테스트 필요 | `07_survivorship_universe.md` |

### 구현 명세에서 제거한 항목

| 항목 | 판단 |
| --- | --- |
| FRED 시리즈 ID 코드 수정 | `fred_collector.py`와 `regime/features.py`가 `VXVCLS`를 사용하고, 코드 검색에서 `VIXREM`, `VXMTSI` 잔여가 운영 코드에 보이지 않음. 구현 명세에서는 삭제한다. |

FRED는 새 구현 대상이 아니라 검증/문서 정리 대상이다. 필요한 작업은 DB 읽기 쿼리로 `raw.fred_series` 잔여 데이터를 확인하고, `AGENTS.md`의 오래된 우선순위 문구를 정리하는 것이다.

---

## 권장 실행 순서

1. `01_postgres_only_and_tests.md`
2. `02_operational_config.md`
3. `03_documentation_secrets.md`
4. `04_sec_collection_scope.md`
5. `05_value_market_cap.md`
6. `06_sector_metadata.md`
7. `07_survivorship_universe.md`

완료된 구현은 `done.md`에 기록하고, `docs/project_open_issues.md`에서 상태를 갱신한다.

