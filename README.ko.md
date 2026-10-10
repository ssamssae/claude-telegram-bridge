# Claude Telegram Bridge

[English](README.md) · [한국어](README.ko.md) · [Web guide · 웹 설명서](https://product.kangdaejong.com/claude-telegram-bridge/) · [Web verification](docs/feature-map.md)

텔레그램의 본인 전용 봇으로 컴퓨터의 Claude에 메시지를 보내고 답변을 받습니다.
브릿지 언어는 한국어·영어 중 고를 수 있습니다. AI 답변의 언어와 모델 설정은 별개입니다.

## 시작하기

```bash
pipx install claude-telegram-bridge
claude-telegram-bridge setup --language ko
```

설정 안내에 따라 BotFather에서 만든 토큰을 입력하고, 본인 봇에 `/start`를 보내세요.
설정 도구가 본인 채팅을 연결하고 서비스와 필요한 세션 연결을 준비합니다.
이미 설치했다면 새로 설정할 필요 없이 봇에 `/language ko`를 보내면 됩니다.

## 언어와 기본 사용

봇 대화에서 다음 명령을 보냅니다.

```text
/language ko
/language en
/language
/ping
```

- `/language ko`: 브릿지 안내와 버튼을 한국어로 표시합니다.
- `/language en`: 영어로 표시합니다.
- `/language`: 현재 선택을 확인합니다.
- `/ping`: 브릿지 연결 응답을 확인합니다.

언어 명령은 브릿지가 처리하며 Claude 입력으로 보내지 않습니다. 선택은 재시작 후에도 유지됩니다.
그다음 평소처럼 요청을 보내면 연결된 Claude 세션에서 처리하고 완료된 답변을 전달합니다.
승인·선택 카드의 안내는 선택한 언어로 보이고, 질문·선택지·모델 이름·코드는 원문을 유지합니다.

## 설정과 문제 확인

처음 사용할 언어는 `.env` 또는 서비스 설정의 `CLB_LANGUAGE=ko`로 지정할 수 있습니다.
이미 대화에서 고른 언어가 있으면 그 선택을 우선합니다. 설정을 바꾸려면 `/language`를 사용하세요.
여러 브릿지를 같은 컴퓨터에서 쓰더라도 언어 선택 파일은 브릿지별로 구분됩니다.

응답이 없으면 프로세스 실행 여부, 본인 채팅 ID, 토큰 파일 경로와 연결한 터미널 세션을 확인하세요.
로그를 공유할 때는 토큰과 대화 내용을 제거하세요. 언어 파일 저장에 실패하면 오류를 알리고 기존 언어를 유지합니다.

## 지원 범위

연결·전달·오류 안내, 승인·선택 카드 설명, 복구·확인 버튼과 주요 설정 응답을 번역합니다.
AI 답변과 사용자 입력을 번역 서비스로 보내지 않으며 별도 번역 비용이 없습니다.
터미널 설치·진단 출력, 고급 운영 보고와 CLI가 직접 만든 문구는 기존 언어를 유지합니다.
자세한 검증 경로는 [언어 설정 문서](docs/i18n.md), 고급 설정은 [영문 문서](README.md)에 있습니다.
