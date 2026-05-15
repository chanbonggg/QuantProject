# SEC 수집 범위 명확화 계획

## 목표

일일 파이프라인의 SEC 수집 범위와 별도 전체 SEC 배치의 역할을 명확히 분리한다.

## 배경

현재 `daily_run.py`는 월 1일에도 `universe[:10]` 샘플만 SEC 수집한다. 이 동작은 빠르지만 전체 S&P500 재무 데이터 갱신으로 오해하기 쉽다.

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/data/collectors/sec_collector.py`
- `scripts/data_collection/collect_sec_all.py`
- `scripts/data_collection/recollect_sec_all.py`
- `README.md`

## 구현 단계

1. 현재 `daily_run.py`의 SEC 수집 정책을 코드와 로그에서 “샘플/헬스체크”로 명확히 표현한다.
2. 전체 SEC 수집은 별도 CLI/배치 명령으로 문서화한다.
3. 필요하면 `--sec-full` 또는 `--skip-sec` 옵션 추가를 검토한다.
4. 전체 수집 시 rate limit, 예상 시간, 실패 재시도 정책을 문서화한다.
5. pipeline result detail에 “샘플 10종목”을 명확히 남긴다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 현재 SEC 수집 정책 기준에 맞게 갱신한다.
- DB 데이터 삭제 없이 dry-run으로 샘플 수집 경로를 확인한다.

샘플 실행:

```powershell
python quant_us/scripts/daily_run.py --date 2026-04-01 --dry-run
```

## 주의사항

- SEC 전체 수집은 오래 걸리고 외부 API rate limit이 있으므로 기본 일일 파이프라인에 무조건 넣지 않는다.
- 수집 실패가 포트폴리오 산출 전체를 막지 않도록 graceful degradation 유지.
