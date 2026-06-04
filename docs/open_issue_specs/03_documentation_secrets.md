# 문서와 비밀정보 정리 명세

## 목표

문서의 API 키, DB 비밀번호, 오래된 Docker/DuckDB 기준을 제거하고 현재 PostgreSQL 기준으로 정리한다.

## 현재 미구현 근거

- `.env.example` 없음
- `AGENTS.md`에 FRED 수정이 아직 다음 우선순위로 남아 있음
- 문서에 과거 Docker/DuckDB 기준 문구가 섞여 있음

## 대상 파일

- `AGENTS.md`
- `README.md`
- `DATA_COLLECTION_GUIDE.md`
- `.env.example`
- `done.md`
- `quant_us/db/init.py`
- 검색 결과에 잡히는 기타 문서

## 구현 요구사항

- 실제 API 키와 DB 비밀번호는 문서에서 placeholder로 바꾼다.
- 현재 운영 기준은 로컬 PostgreSQL `127.0.0.1:5432/quant_us`로 통일한다.
- Docker `5433` 기준은 현재 운영 가이드에서 제거하고, 과거 이력은 `done.md`에만 둔다.
- DuckDB는 전환 대상 또는 과거 구조로만 설명한다.
- `quant_us/db/init.py`의 기본 DSN이 문서와 충돌하지 않게 한다.
- FRED ID 수정은 구현 완료로 정리하고, DB 잔여 확인만 남긴다.

## 완료 기준

- 공개 문서에 실제처럼 보이는 API 키나 DB 비밀번호가 없다.
- `.env.example`을 보고 `.env`를 만들 수 있다.
- `AGENTS.md`의 다음 우선순위가 실제 코드 상태와 맞다.

## 검증 명령

```powershell
rg "rlacksdud|FRED_API_KEY=.*[A-Za-z0-9]{10,}|postgres:quant@|127.0.0.1:5433|VIXREM|VXMTSI" AGENTS.md done.md README.md DATA_COLLECTION_GUIDE.md .env.example quant_us -n
```

