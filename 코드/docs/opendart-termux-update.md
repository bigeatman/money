# 기존 Termux에 OpenDART v3 적용

현재 사용 중인 `~/money-work/money/코드`의 OpenDART v2를 업데이트하는 절차입니다. 휴대폰에서 아래 작업을 실행해야 실제로 적용됩니다. GitHub에 코드를 올린 것만으로 휴대폰 코드가 바뀌지는 않습니다.

## 1. Termux에서 업데이트 실행

아래 블록 전체를 한 번 붙여 넣습니다. 기존 Python과 tmux를 사용합니다. `curl: command not found`가 나오면 `pkg install curl`을 실행한 뒤 다시 붙여 넣습니다.

```bash
(
set -e
cd "$HOME/money-work/money/코드"
curl -fL --retry 3 \
  'https://raw.githubusercontent.com/bigeatman/money/codex/opendart-cache/updates/money_dart_v3_20261019.py' \
  -o "$HOME/money_dart_v3_20261019.py"
printf '%s  %s\n' \
  '7673f9b5cbb55fa64239b81ace5e9ce75e39df9e6930e728b8f01693728ca5ea' \
  "$HOME/money_dart_v3_20261019.py" | sha256sum -c -
python "$HOME/money_dart_v3_20261019.py" "$PWD"
)
```

업데이트 파일은 다음 작업을 수행합니다.

1. 관리 대상 소스가 알려진 v2 또는 이번 v3 코드인지 SHA-256으로 검사합니다. 다른 수정이 있으면 덮어쓰지 않습니다.
2. 기존 `money` tmux 서버를 정상 종료합니다.
3. 바뀌는 소스와 DART SQLite DB를 `runtime/code-backups/<시각>_dart_v3/`에 백업합니다. DB의 WAL 내용도 백업에 포함합니다.
4. 소스를 교체하고 전체 오프라인 테스트를 실행합니다.
5. 서버를 다시 시작하고 실제 서버의 `opendart-v3`, `schema_version=2`, API 키 설정, 원문 저장 기능과 `query_dart_data` DB 조회를 확인합니다.

자체 테스트가 실패하면 소스를 복구합니다. 새 서버의 시작·적용 확인이 실패하면 이전 소스와 DART DB 백업을 복구합니다. 기존 `.env`, `config.json`, 매매 장부는 업데이트 대상에 포함되지 않습니다. API 키 재입력, 신규 초기화, Git 브랜치 변경·병합은 필요하지 않습니다. 이 업데이트와 적용 확인은 DART API를 호출하지 않습니다.

성공하면 다음 문구가 나옵니다.

```text
적용 확인: opendart-v3 / schema_version=2 / configured=true
```

같은 업데이트 파일을 다시 실행해도 이미 적용된 소스는 중복 변경하지 않습니다. 기존 파일 불일치나 미확정 매수 주문 때문에 중단되면 출력된 오류를 전달합니다. 임의로 장부나 설정을 지우지 않습니다.

## 2. 기존 DB 자료를 원본 파일 저장 체계로 이관

업데이트 직후부터 새 API 응답은 파일·체크섬·DB 필드로 저장됩니다. 예전에 DB에만 저장한 자료는 아래 명령으로 전체 파일 이관합니다.

```bash
cd "$HOME/money-work/money/코드"
python -m money.cli dart-migrate --limit 100 --all
python -m money.cli dart-status
```

`100`은 한 번에 처리하는 배치 크기이며 총 처리 한도가 아닙니다. 진행 상황을 출력하고, `Ctrl+C`로 중단했더라도 같은 명령으로 이어서 처리합니다. 이관 중에는 추가 DART 수집을 시작하지 않습니다. 네트워크 호출은 없습니다.

완료 기준은 이관 결과 `complete: true`, `remaining: 0`이며, 상태의 `archive.legacy_versions_pending: 0`으로도 확인합니다.

과거 ZIP 응답은 기존 DB의 바이트를 그대로 파일로 옮깁니다. 과거 JSON은 v2가 파싱한 값을 저장했으므로 당시 공백·줄바꿈까지 복원할 수 없고 `legacy_reconstructed_json`으로 표시합니다. v3로 새로 받은 응답부터 HTTP 본문 그대로 저장합니다. 과거 JSON의 정확한 HTTP 원본이 필요하면 해당 요청을 `refresh=true`로 다시 호출해야 합니다.

## 3. ChatGPT money 연결의 도구 목록 새로 고침

ChatGPT의 money 연결에서 도구 목록을 새로 고침합니다. 실제 화면의 버튼 명칭은 앱 버전에 따라 다를 수 있습니다. 새 `query_dart_data`가 나타나야 합니다. 기존 API 키나 비밀 연결 URL을 대화에 붙여 넣을 필요는 없습니다.

Termux 성공 출력과 도구 목록 새로 고침 여부를 대화에 알려 주면, assistant가 연결된 실제 서버 버전과 DB 상태를 다시 확인한 뒤 중단한 10개 종목 수집을 이어갑니다. 코드 업데이트만으로 10개 종목 수집이나 69개 검토 영역의 분석이 완료되는 것은 아닙니다.

## 저장 위치

| 용도 | 경로 |
| --- | --- |
| 운영 코드 | `~/money-work/money/코드/` |
| DART 데이터·체크섬·요청 이력 | `~/money-work/money/코드/runtime/fundamentals.sqlite` |
| 원본·ZIP 내부 파일 | `~/money-work/money/코드/runtime/fundamentals.files/objects/` |
| 업데이트 전 소스·DART DB 백업 | `~/money-work/money/코드/runtime/code-backups/<시각>_dart_v3/` |

정상 작동 후의 전체 DART 백업은 다음과 같이 만듭니다. 같은 경로를 덮어쓰지 않으므로 매번 다른 이름을 지정합니다.

```bash
python -m money.cli dart-export --file "$HOME/dart-backup-20261019.sqlite"
```

이 명령은 `dart-backup-20261019.sqlite`와 `dart-backup-20261019.files/`를 함께 만듭니다. 복원·이동 시 두 항목을 함께 보관합니다. 자세한 수집·조회 명령은 [OpenDART 안내](opendart.md)를 참고합니다.
