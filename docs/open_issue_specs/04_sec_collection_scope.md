# SEC 수집 범위 명확화 명세

## 목표

일일 파이프라인의 SEC 단계가 샘플/헬스체크인지 전체 수집인지 명확히 분리한다.

## 현재 미구현 근거

- `daily_run.py`에 `--skip-sec`, `--sec-sample-size`, `--sec-full` 옵션 없음
- SEC 단계 주석이 전체 유니버스 증분 수집처럼 읽힘
- 실제 코드는 제한된 샘플만 처리하는 흐름으로 보임

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/data/collectors/sec_collector.py`
- `scripts/data_collection/collect_sec_all.py`
- `scripts/data_collection/recollect_sec_all.py`
- `README.md`

## CLI 명세

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--skip-sec` | false | SEC 단계를 완전히 건너뜀 |
| `--sec-sample-size N` | `10` | daily SEC 헬스체크 샘플 크기 |
| `--sec-full` | false | 전체 SEC 수집 실행. 명시적으로 지정할 때만 수행 |

## 구현 요구사항

- 기본 daily pipeline은 빠른 샘플/헬스체크 역할을 유지한다.
- 로그와 pipeline result detail에 `mode`, `sample_size`, `tickers_processed`를 남긴다.
- 전체 수집은 기본 daily 실행에 자동 포함하지 않는다.
- `--sec-full`은 rate limit, 재시도, 예상 소요 시간 문서화 후 사용한다.
- SEC 실패가 포트폴리오 산출 전체를 막지 않도록 graceful degradation을 유지한다.

## 완료 기준

- daily SEC 로그가 전체 수집으로 오해되지 않는다.
- README에 daily 샘플 수집과 전체 SEC 배치가 분리되어 있다.
- dry-run에서 SEC mode와 sample size가 확인된다.

## 검증 명령

```powershell
python quant_us/scripts/daily_run.py --date 2026-04-01 --dry-run
python quant_us/scripts/daily_run.py --date 2026-04-01 --dry-run --skip-sec
python quant_us/scripts/daily_run.py --date 2026-04-01 --dry-run --sec-sample-size 5
```

