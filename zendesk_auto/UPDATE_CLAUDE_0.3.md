# Claude API 전환 업데이트 0.3

0.2에 적용하는 코드 업데이트입니다. Zendesk 티켓은 읽기·추천 전용이며 실제 담당자 지정, 상태 변경, 공개 답변 기능은 포함하지 않습니다.

## 바뀐 부분

- Claude Messages API의 구조화된 JSON 출력으로 문의 요약·파트·판단 근거를 생성합니다. AI 결과와 배정 규칙이 충돌하면 검토합니다.
- `.env`에 `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`을 사용합니다. `ANTHROPIC_WORKSPACE_ID`는 계정에서 별도로 요구하는 경우에만 입력합니다.
- 이전 `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_EMBEDDING_MODEL` 설정은 더 이상 사용하지 않으며, 환경변수에 남아 있어도 OpenAI에 연결하지 않습니다.
- 과거 완료 사례는 문자 유사도로 검색합니다. 이 업데이트는 Claude 임베딩이나 의미 검색을 구현하지 않습니다.
- 기존 설정, Google Sheets, 담당자, 일정, 티켓 이력과 `data`, `.env`, 인증파일, `.venv`는 ZIP에 포함하지 않습니다.

## 설치

1. 기존 프로그램을 Ctrl+C로 중지합니다.
2. 업데이트 ZIP을 기존 프로젝트 폴더에 복사합니다.
3. `cd ~/zendesk_auto/zendesk_auto` 후 `unzip -o Zendesk_Claude_update_0.3.zip -d .`를 실행합니다. 코드는 `app/clients.py`, `app/main.py`, `app/engine.py`, `app/static/app.js`를 교체합니다.
4. `.env`에 아래 항목을 추가하거나 수정하고 서버를 재시작합니다. 다른 기존 설정을 지우지 마세요.

```dotenv
ANTHROPIC_API_KEY=발급받은_Claude_API_키
ANTHROPIC_MODEL=claude-sonnet-4-6
ANTHROPIC_WORKSPACE_ID=
```

위 모델은 예시이며 사용 권한과 가격은 해당 계정에서 확인하세요. Claude 구독과 API 비용은 별도일 수 있습니다. 워크스페이스 범위가 없는 키에만 워크스페이스 ID가 추가로 필요할 수 있습니다.

5. 웹에서 Ctrl+F5로 갱신하고 연결 및 설정에서 Claude 분석을 활성화합니다. 실제 티켓 자동 수집을 활성화하기 전에 환자정보가 없는 예제 문의로 추천 테스트를 수행하세요. 실제 알림에는 Telegram Bot 토큰과 대화방 ID가 별도로 필요합니다.

## 보안과 검증

기존 패턴 마스킹 후 문의·유사 사례를 외부 Claude API에 전송합니다. 패턴 처리와 모델 지시만으로 모든 개인정보 제거를 보장할 수는 없습니다. 생성된 요약과 근거가 Telegram으로 전송되므로 실제 운영 전에 데이터 비식별화와 수신 대화방에 대한 사내 정책을 확인하세요.

API 인증·요금·호출 제한·모델 미접근·응답 중단·거부·스키마 오류는 자동 배정을 중단하고 기존 재시도 후 검토 대상으로 전환합니다. API 키를 오류 메시지에 출력하지 않습니다.

테스트는 HTTP 응답을 모의하여 수행했으며 Claude, Zendesk, Google Sheets, Telegram의 실제 연결은 검증하지 않았습니다. 문의를 AI API에 전송하면 과금될 수 있습니다. 실제 미해결 업무량은 Zendesk 담당자/필드 매핑을 완료해야 정확합니다.

참고: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
