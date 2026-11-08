# Claude Telegram Bridge

[English](README.md) · [한국어](README.ko.md) · [Web guide · 웹 설명서](https://product.kangdaejong.com/claude-telegram-bridge/) · [Web verification](docs/feature-map.md)

텔레그램의 본인 전용 봇으로 컴퓨터의 Claude에 메시지를 보내고 답변을 받습니다.
브릿지 언어는 한국어·영어 중 고를 수 있습니다. AI 답변의 언어와 모델 설정은 별개입니다.

## 시작하기

```bash
pipx install claude-telegram-bridge
claude-telegram-bridge setup --language ko
```

`pipx`가 없으면 먼저 설치하고 터미널을 다시 여세요.
우분투·데비안·WSL은 `sudo apt update && sudo apt install pipx`를 실행한 뒤 `pipx ensurepath`를 실행합니다.
macOS(Homebrew)는 `brew install pipx && pipx ensurepath`를 실행합니다.
`pipx` 패키지가 없는 옛 배포판에서만 `python3 -m pip install --user pipx`와 `python3 -m pipx ensurepath`를 쓰세요.
우분투 24.04 이상은 이 명령을 PEP 668(`externally-managed-environment`)로 막습니다.

설정 안내에 따라 BotFather에서 만든 토큰을 입력하고, 본인 봇에 `/start`를 보내세요.
설정 도구가 본인 채팅을 연결하고 서비스와 필요한 세션 연결을 준비합니다.
이미 설치했다면 새로 설정할 필요 없이 봇에 `/language ko`를 보내면 됩니다.

## 제작자와 똑같이 쓰기 (한 줄 설치)

```bash
pipx install claude-telegram-bridge && claude-telegram-bridge setup --like-author --language ko
```

사람 손이 필요한 일은 둘뿐입니다. BotFather에서 봇을 만들어 토큰을 받는 것과, 그 봇에 `/start`를 보내는 것.
나머지는 자동입니다. 설정 파일, 아래 스위치 4개, 브릿지 훅 전부, 상시 서비스와 워치독, tmux 세션과 그 안의 Claude 실행,
그리고 폰→터미널→폰·터미널→폰 두 방향을 실제로 한 번씩 돌려 보는 자가 점검까지 합니다.
대화식 설정 도구도 첫 질문으로 `Use the author's setup (recommended)? [Y/n]`을 묻고 기본값은 예입니다.

| 스위치 | 켜지는 것 |
| --- | --- |
| `CLB_BUSY_INJECT=1` | Claude가 바쁜 중에도 폰 글이 입력줄에 바로 올라가고, 오래 기다리면 바로 보내기 버튼이 붙음 |
| `CLB_FLOW_MIRROR=1` | 작업 흐름 카드와 그 카드의 중지 버튼, 터미널에서 친 글의 터미널 입력·결과 카드 |
| `CLB_ASKUSER_CARDS_ENABLED=1` | 터미널의 질문 창이 폰에 답할 수 있는 카드로 옴 |
| `CLB_SUGGESTED_LOOP=1` | 추천 답변을 취소 창이 지난 뒤 자동으로 보냄 |

그다음부터는 어느 터미널에서든 `claude-tg`를 치면 브릿지가 보는 바로 그 Claude 세션에 붙습니다. 폰과 터미널이 같은 세션을 씁니다.
따로 연 Claude 창은 폰과 연결되지 않으며, `claude-telegram-bridge doctor`가 그런 창을 보면 경고합니다.
`setup --like-author --dry-run`은 아무것도 바꾸지 않고 할 일만 보여 줍니다. 서비스 파일 경로와 이미 있어서 바꿀 파일 목록도 보여 줍니다.
훅은 설치한 패키지에 든 파일을 복사하므로 설치한 버전과 같고, 설정 도구는 아무것도 내려받지 않습니다.
같은 이름의 서비스 파일을 이 설정 도구가 만들지 않았으면, 설정 폴더에 `<이름>.bak-<시각>` 사본을 남긴 뒤 바꿉니다. 대화식 설정은 먼저 묻습니다.
사본은 서비스 폴더 밖에 두므로 launchd·systemd가 사본을 다시 읽지 않습니다.
`uninstall`은 이 설정 도구가 만든 서비스 파일만 지우고 백업은 남깁니다.
설정 도구는 서비스보다 tmux 세션을 먼저 만들어, 브릿지가 처음 켜질 때 세션을 바로 찾습니다.
Claude는 설정 도구를 실행한 폴더에서 켜집니다. 홈 폴더에서 실행하면 대신 `~/claude-workspace`에서 켜집니다. Claude Code는 홈 폴더에서는 켤 때마다 폴더 신뢰를 다시 묻기 때문입니다.
다른 폴더를 쓰려면 `setup --workdir <폴더>`를 씁니다. 설정 도구가 이 폴더를 `CLB_SESSION_WORKDIR`로 저장하고 `claude-telegram-bridge attach`도 같은 폴더를 씁니다.
재부팅 뒤처럼 세션이 없을 때 서비스가 켜지면, 브릿지는 꺼지지 않고 세션이 생길 때까지 기다립니다.
자가 점검은 Claude가 폴더 신뢰 질문·로그인 화면·`API Usage Billing` 배너에서 멈춰 있으면 무엇을 누를지 알려 줍니다.

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
