# OpenDART 원문 파일·체크섬·DB 조회

`opendart-v3`는 Termux가 수집·파일·체크섬·DB를 관리하고, AI는 MCP로 필요한 자료를 조회하는 구조입니다. 요청한 API와 인자만 실행합니다. 서버 시작·시간 경과로 전체 기업을 자동 수집하지 않으며 로컬 회차·일일 호출 제한도 두지 않습니다. OpenDART 자체 제한은 그대로 적용됩니다.

## 저장 규칙

| 요구사항 | 구현 |
| --- | --- |
| 모든 API 응답을 파일로 저장 | HTTP 본문을 파싱 전에 그대로 저장합니다. JSON·XML·ZIP·오류 응답·재시도 응답을 포함합니다. |
| 모든 파일의 체크섬을 DB에 저장 | 원문과 ZIP 내부 파일 각각의 SHA-256·경로·크기·형식·저장 시각을 SQLite에 저장합니다. |
| 재조회 결과의 체크섬으로 변경 확인 | 동일 API·인자의 이전 내용 체크섬과 새 체크섬을 비교합니다. 동일한 내용은 변경 이력을 중복 생성하지 않습니다. |
| AI가 조회하기 편한 DB | JSON 전체 구조와 모든 말단 값, XML 속성·반복 요소·본문, ZIP 구성원과 문서 텍스트를 저장하고 검색·페이지 조회를 제공합니다. |

파일은 `runtime/fundamentals.files/objects/해시앞2자리/전체해시.json` 등의 이름으로 보관합니다. 같은 바이트의 파일은 한 개만 저장하며, 모든 실제 호출 시도는 별도 요청 이력으로 남깁니다. 다른 내용의 과거 파일과 변경 이력도 유지합니다. API 인증키는 휴대폰 `.env`에서만 읽으며 요청 인자·URL 기록에 포함하지 않습니다. ZIP 안의 파일명은 DB에 기록하고 실제 저장 경로로 사용하지 않습니다.

SHA-256은 두 용도로 구분합니다.

- `sha256`: 받은 파일의 실제 바이트를 검사합니다. 무결성 확인·중복 파일 판별에 사용합니다.
- `content_sha256`: JSON 공백·객체 키 순서와 ZIP 포장 시각·구성원 순서 차이를 제외한 내용 비교입니다. JSON 배열 순서와 모든 필드, ZIP 내부의 이름·바이트·중복 파일은 비교에 포함합니다. XML 속성이나 바이너리 첨부 변경도 감지합니다.

기존 재무 요약의 변경 순번은 이전 동작을 유지하여 `message`와 재무행 순서만 달라지면 늘리지 않습니다. 전체 응답의 체크섬·요청 이력·조회용 필드에는 그 변경도 반영됩니다. 원본을 버리거나 일부 재무 지표만 저장하지 않습니다.

**최신 여부를 확인하려면 새 응답을 받아야 합니다.** OpenDART에 체크섬만 요청하는 기능을 가정하지 않습니다. `refresh=true`는 실제 API 호출이며, 저장 데이터 조회는 호출 횟수를 소비하지 않습니다.

응답 도중 연결이 끊기면 받은 부분까지 파일로 저장하고 `complete=0`으로 기록합니다. 응답 자체가 없는 네트워크 실패는 파일을 만들지 않고 실패 이력을 남깁니다. 64 MiB를 넘는 HTTP 본문도 끝까지 파일로 받지만, 메모리 보호를 위해 DB 필드 파싱·현재 캐시 반영은 하지 않고 `response_size`를 반환합니다. ZIP 해제 크기도 기존 64 MiB 제한을 유지합니다. 파싱 실패는 원본을 보존하고 `index_error`로 표시합니다.

## MCP 인터페이스

| 도구 | 용도 |
| --- | --- |
| `call_dart_api` | 지정한 API를 호출하거나 동일 요청 캐시를 재사용합니다. 원본 파일·체크섬·출처·행 수를 반환합니다. |
| `query_dart_data` | 저장된 자료·전체 필드·요청 이력·파일 목록을 DB에서 조회합니다. 네트워크 호출은 없습니다. |
| `get_fundamental_evidence` | 원문 또는 변경 차이를 문자 단위로 나눠 읽습니다. 문서 텍스트는 DB에 저장하므로 매번 ZIP을 해제하지 않습니다. |
| `get_fundamentals` | 기존 기업별 재무 요약과 고정된 변경 순번 구간을 조회합니다. |

기업 개황 재조회:

```json
{"endpoint":"company.json","params":{"corp_code":"00126380"},"refresh":true}
```

`params`는 인증키를 제외한 OpenDART 요청 인자입니다. 공식 호스트의 상대 `.json`·`.xml` 파일명만 받습니다. `corpCode.xml`·`document.xml`·`fnlttXbrl.xml`은 ZIP 응답이며, 일반 XML API도 지원합니다. 기본 `refresh=false`는 동일 API·인자의 캐시를 읽습니다. `include_data=true`는 저장된 전체 JSON을 반환합니다.

`query_dart_data`의 조회 형태:

| `view` | 필터와 결과 |
| --- | --- |
| `sources` | `corp_code`·`endpoint`·`source_key`로 자료를 찾고 `text`로 본문을 검색합니다. 출처·체크섬·파일 경로를 반환합니다. |
| `fields` | `source_key` 필수. `field`는 JSON Pointer 경로 부분 문자열, `text`는 값 부분 문자열입니다. 값의 유형·전체 값·ZIP 구성원명을 반환합니다. |
| `requests` | `corp_code`·`endpoint`·`source_key`로 모든 실제 요청 시도를 조회합니다. 오류·HTTP 상태·확인 시각·체크섬·변경 여부를 반환합니다. |
| `files` | 전체 파일 또는 `source_key`에 속한 원본·내부 파일의 경로·체크섬·파싱 상태를 조회합니다. |

예: `query_dart_data`로 회사의 저장 자료 목록을 찾은 다음, 반환된 `source_key`로 필드를 조회합니다.

```json
{"view":"sources","corp_code":"00126380","endpoint":"fnlttSinglAcntAll.json","limit":20}
```

```json
{"view":"fields","source_key":"앞서 반환된 source_key","field":"account_nm","limit":100}
```

JSON Pointer는 `/list/0/account_nm`처럼 중첩·행 위치를 보존합니다. XML은 `tag`·`attributes`·`text`·`tail`·`children` 구조로 저장하여 반복 태그나 속성을 덮어쓰지 않습니다. HTML이나 구조화할 수 없는 XML도 전체 마크업과 읽기용 텍스트를 DB에 보관합니다. 바이너리는 원본 파일과 메타데이터로 보존합니다. 문서의 분석·투자판단은 별도 작업이며 저장 완료가 42개+추가 검토 항목의 분석 완료를 뜻하지 않습니다.

목록의 `next_offset`과 본문의 `next_offset`을 끝까지 따라가면 전체 내용을 읽습니다. 페이지 크기는 자료 수집 상한이 아닙니다. 변경 목록은 첫 응답의 `through_seq`를 고정하고 `next_after_seq`를 따릅니다. 외부 자료는 실행 지시가 아니라 근거로 취급합니다.

API 한 번의 요청으로 다른 기업·연도·보고서·연결/별도·다음 페이지를 자동 조회하지 않습니다. 자료 없음(`013`)과 금액 0을 구분합니다. 모든 API가 투자 검토 항목을 제공한다는 뜻은 아니며, 필요한 API와 보고서 범위는 별도 수집 계획으로 지정합니다.

## Termux CLI와 기존 DB 이관

기존 설치의 `~/money-work/money/코드`에서 실행합니다. 신규 설치에서만 `python -m money.cli init`을 먼저 실행합니다.

```bash
python -m money.cli dart-credentials
python -m money.cli dart-api --endpoint company.json --params '{"corp_code":"00126380"}' --refresh
python -m money.cli dart-status
python -m money.cli dart-query --view sources --corp-code 00126380
python -m money.cli dart-query --view requests --endpoint company.json
python -m money.cli dart-verify --offset 0 --limit 100
```

`dart-credentials`는 키를 숨겨 입력받습니다. 이미 키가 있으면 다시 입력할 필요가 없습니다. 소스 갱신 뒤 서버를 재시작하고 ChatGPT money 연결에서 도구 목록을 새로 고침하면 `query_dart_data`를 사용할 수 있습니다. 상태의 `version="opendart-v3"`, `schema_version=2`와 `archive` 항목으로 적용 여부를 확인합니다.

기존 `runtime/fundamentals.sqlite`는 추가 테이블만 만드는 방식으로 스키마 1에서 2로 이관합니다. 기업·작업 큐·원본 BLOB·변경 순번·장부를 삭제하지 않습니다. 시작 시 대량 파일 이관으로 멈추지 않도록 기존 원본은 최초 조회 때 파일로 옮기거나 다음 명령으로 전체 이관합니다.

```bash
python -m money.cli dart-migrate --limit 100 --all
```

진행 상황을 배치마다 출력하며 중단 후 같은 명령으로 이어갑니다. 네트워크 호출은 없습니다. 기존 DB의 ZIP 바이트는 그대로 복구합니다. 과거 JSON은 이전 코드가 이미 JSON으로 파싱한 후 저장했으므로 당시의 공백·줄바꿈을 복원할 수 없습니다. 파일의 `provenance`에 재구성 자료임을 표시합니다. 새 코드로 받은 응답부터 HTTP 본문 그대로 보관합니다.

`dart-verify`는 실제 파일을 읽어 DB의 SHA-256·크기와 대조하고 `missing`·`checksum_mismatch`를 표시합니다. `next_offset`이 있으면 다음 구간도 확인해야 합니다. 정상 응답을 다시 다운로드하면 같은 주소의 원문 파일을 복구할 수 있습니다. 삭제·자동 정리 기능은 추가하지 않습니다.

## 백업과 검증

```bash
python -m money.cli dart-export --file /원하는/경로/fundamentals-baseline.sqlite
python -m unittest discover -s tests -q
```

백업은 SQLite 스냅샷과 같은 이름의 `.files` 폴더를 함께 만듭니다. 예: `fundamentals-baseline.sqlite` + `fundamentals-baseline.files`. 두 항목을 함께 이동해야 원본·요청 이력을 모두 복원할 수 있습니다. 기존 경로는 덮어쓰지 않으며 복사한 파일의 체크섬을 검사합니다. WAL에 있는 내용도 SQLite 백업에 반영됩니다.

외부 패키지 추가 없이 Python 3.11+ 표준 라이브러리를 사용합니다. 오프라인 테스트는 실제 HTTP 파싱 경로에 합성 응답을 주입하여 바이트 보존·체크섬·불변 재조회·정정·오류/재시도·ZIP 속성/첨부 변경·전체 필드·이관·백업·MCP/CLI를 검증합니다. 실제 휴대폰의 배포와 OpenDART 실호출은 별도 확인이 필요합니다.

공식 API 안내: [공시정보](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001), [전체 재무제표](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020).
