# FRED 시리즈 ID 수정 계획

상태: 완료 (2026-05-10)

## 목표

FRED에 존재하지 않거나 잘못된 변동성 시리즈를 정리해 레짐 피처의 `vix3m`과 `vix_term`이 실제 데이터로 계산되도록 한다.

## 배경

기존 코드에는 `VIXREM`, `VXMTSI`가 남아 있었다. `VIXREM`은 잘못된 ID이며, `VXMTSI`는 FRED에서 사용할 수 없는 시리즈다. 이 상태에서는 FRED 수집 실패 로그가 반복되고, VIX term structure 기반 레짐 판단이 약해진다.

## 대상 파일

- `quant_us/data/collectors/fred_collector.py`
- `quant_us/regime/features.py`
- 관련 문서: `README.md`, `DATA_COLLECTION_GUIDE.md`

## 구현 단계

1. 완료: `fred_collector.py`의 `FRED_SERIES`에서 `VIXREM`을 `VXVCLS`로 교체했다.
2. 완료: `fred_collector.py`의 `FRED_SERIES`에서 `VXMTSI`를 제거했다.
3. 완료: 파일 상단 설명을 13개 시리즈에서 12개 시리즈 기준으로 갱신했다.
4. 완료: `features.py`의 `FRED_SERIES_MAP["vix3m"]`을 `VXVCLS`로 교체했다.
5. 완료: 테스트 fixture의 `VIXREM` 값을 `VXVCLS`로 교체했다.
6. 완료: 문서에 남아 있는 운영 기준 설명을 현재 기준으로 정리했다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 현재 FRED 시리즈 기준에 맞게 갱신한다.
- DB 데이터 삭제 없이 읽기 쿼리 또는 dry-run으로 동작을 확인한다.

FRED 수집 확인:

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from data.collectors.fred_collector import collect_all; print(collect_all('2026-04-01'))"
```

## 주의사항

- 기존 `raw.fred_series`에 `VIXREM` 데이터가 없거나 쓸모없는 경우가 많다. 삭제는 별도 승인 전 하지 않는다.
- DB 데이터 삭제 없이 새 `VXVCLS` 수집만 진행한다.
