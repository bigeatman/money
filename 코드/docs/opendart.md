# OpenDART 요청형 API 연결·저장

`opendart-v2`는 사용자가 요청한 API와 인자만 실행합니다. 서버 시작·시간 경과만으로 전체 기업을 수집하지 않습니다. 프로그램의 회차·일일 호출 횟수 제한은 없으며, 기존 `config.json`에 남은 `max_requests_per_run`·`max_requests_per_day`도 무시합니다. 요청 횟수는 통계로만 기록합니다. OpenDART가 반환하는 제한·인증·전송 오류는 그대로 구분해 반환합니다.

## MCP 사용

| 도구 | 동작 |
| --- | --- |
| `call_dart_api` | 지정한 API 요청 하나를 실행하거나 동일 요청의 캐시를 읽고, 전체 응답과 변경 이력을 SQLite에 저장합니다. |
| `get_fundamental_evidence` | 반환된 `source_key` 또는 변경 목록의 `diff_key`에 해당하는 원문을 필요한 구간만 읽습니다. |
| `get_fundamentals` | 기존 기업 캐시 요약과 순번 이후의 변경 목록을 읽습니다. 새 요청형 응답은 반환된 `source_key`로 읽습니다. |

예시: 기업 개황 요청

```json
{"endpoint":"company.json","params":{"corp_code":"00126380"},"refresh":true}
```

예시: 2025년 사업보고서의 연결 재무제표만 요청

```json
{"endpoint":"fnlttSinglAcntAll.json","params":{"corp_code":"00126380","bsns_year":"2025","reprt_code":"11011","fs_div":"CFS"},"refresh":true}
```

`params`는 OpenDART의 요청 인자입니다. 인증키는 휴대폰의 `.env`에서 읽으므로 인자로 전달하지 않습니다. 공식 호스트의 상대 API 파일명만 받으며 다른 URL이나 인증값을 받지 않습니다. JSON API와 원본 ZIP API(`corpCode.xml`, `document.xml`, `fnlttXbrl.xml`)를 지원합니다.

기본 `refresh=false`는 완전히 같은 API·인자의 저장 데이터를 재사용합니다. 최신 자료를 다시 확인하려면 `refresh=true`를 사용합니다. 응답은 기본적으로 출처·저장 시각·API 상태·행 수·공급자 페이지 정보만 반환합니다. `include_data=true`이면 전체 JSON도 반환합니다. ZIP 원본은 저장 후 `source_key`로 텍스트를 읽습니다. 대량 본문을 모델에 모두 보내지 않고 필요한 근거만 읽을 수 있습니다.

한 API 요청으로 다른 기업·연도·보고서 유형·연결/별도·다음 페이지·공시 원문을 추가 조회하지 않습니다. OpenDART 응답에 페이지 정보가 있으면 호출자가 원하는 다음 페이지를 별도로 요청합니다. 자료 없음(`013`)은 금액 0과 다릅니다.

호출과 저장은 주문 장부·증권사 주문에 접근하지 않습니다. 전체 수집을 시작하던 `start_fundamental_sync` MCP 도구와 `dart-sync` CLI는 공개 인터페이스에서 제거했습니다. 기존 작업 큐와 자료는 보존되며 서버를 재시작해도 자동 실행되지 않습니다. 이전 `run_budget` 정지는 이력에 보관하고 해제합니다.

## 휴대폰 설정·CLI

기존 설치의 `코드` 디렉터리에서 실행합니다. 키는 그대로 사용하며 새 설치에서만 먼저 `python -m money.cli init`을 실행합니다.

```bash
python -m money.cli dart-credentials
python -m money.cli dart-api --endpoint company.json --params '{"corp_code":"00126380"}' --refresh
python -m money.cli dart-status
```

`dart-credentials`는 키를 화면에 표시하지 않고 `.env`의 `OPENDART_API_KEY`에 저장합니다. 키 입력 뒤 이미 실행 중인 서버는 재시작해야 읽습니다. 키·설정·기존 DB를 Git에 올리지 않습니다.

상태의 `collection_mode="on_demand"`, `automatic_collection=false`, `local_request_limits={"per_run":null,"per_day":null}`로 적용 여부를 확인합니다. `version`은 `opendart-v2`입니다.

## 저장·변경분·내보내기

운영 DB는 기존 `runtime/fundamentals.sqlite`를 사용합니다. 인증키는 저장하지 않습니다. API 파일명·인자·확인 시각, 압축한 원본, 변경 전 버전과 필드별 차이를 보존합니다. 요청 인자가 다르면 별도 자료이며, 재조회 결과가 같으면 변경 이력을 중복 생성하지 않습니다. 파일 스키마는 기존 `PRAGMA user_version=1`을 유지합니다.

기존의 기업·공시·재무제표·원문과 변경 순번은 삭제하지 않습니다. 저장된 자료가 투자 검토 완료를 뜻하지 않으며 `investment_review_complete=false`입니다. API별로 필요한 데이터가 있는지 별도로 확인해야 합니다.

수집 중 SQLite의 WAL 보조 파일이 생길 수 있습니다. 아래 명령은 최신 WAL까지 반영한 단독 SQLite 파일을 생성하며 기존 내보내기 파일을 덮어쓰지 않습니다.

```bash
python -m money.cli dart-export --file /원하는/경로/fundamentals-baseline.sqlite
python -m money.cli dart-show --after-seq 120 --limit 20
```

변경 목록을 여러 페이지 읽을 때 첫 응답의 `through_seq`를 유지하고 `next_after_seq`를 따라갑니다. 실제 검토를 마친 순번만 다음 검토 기준으로 저장합니다. `get_fundamental_evidence`의 텍스트는 외부 자료이며 실행 지시로 취급하지 않습니다.

## 배포·검증

소스를 갱신한 뒤 프로그램 서버를 재시작합니다. Cloudflare 터널은 계속 켜두면 기존 연결 주소를 유지할 수 있습니다. ChatGPT의 기존 money 연결에서 `Refresh`로 MCP 도구 목록을 갱신해야 `call_dart_api`가 표시됩니다.

```bash
python -m unittest discover -s tests -q
```

오프라인 합성 응답으로 기존 데이터 유지, 로컬 횟수 제한 제거, 요청한 API만 실행, 캐시 재사용·명시적 재조회·변경 이력, 공급자 오류, 키 보호 및 MCP 라우팅을 검증합니다. 실제 키를 사용하는 휴대폰 적용·API 응답은 별도로 확인해야 합니다.

공식 API 안내: [공시정보](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001), [전체 재무제표](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020).
