#!/usr/bin/env bash
# composer-clear.sh — composer(tmux pane 입력창) 전체를 비우는 멀티라인 clear 프리미티브.
# T-260723-044 (L0), 설계 SoT = docs/specs/2026-07-23-composer-occupancy-detection.md §5.
#
# 문제 (§1.3): 현행 /clear 경로 3개는 전부 줄 단위 편집키(C-u/C-a/C-k)라 커서 있는 1줄만
#   지운다. 멀티라인 잔여 위에 /clear 가 붙으면 TUI 가 명령으로 안 보고 평문 제출 →
#   세션 미삭제 + 오염 턴 배달. "지연 재시도"도 지연 끝에 비울 수단이 없으면 성립 안 한다.
#
# 이 프리미티브가 하는 일:
#   커서를 현재 줄 끝으로 보낸 뒤(C-e) `(C-u BSpace)` 를 max-lines 회 반복한다.
#     - C-u  : 현재 논리 줄을 줄머리까지 제거(줄 단위 kill).
#     - BSpace: 줄머리(col 0)에서 앞 줄과의 개행을 지워 윗줄로 병합, 커서는 윗줄 끝으로.
#   → 아래→위로 한 줄씩 소거해 멀티라인 잔여를 전부 비운다. 빈 버퍼에선 no-op(bounded).
#
# 전제: 커서가 버퍼 끝에 있다(붙여넣기·타이핑 잔여의 실제 상태 — 기존 clear 코드도 C-e 로
#   같은 전제를 쓴다). 커서가 중간이고 그 아래로 줄이 더 있는 상황은 실제 writer 경로에서
#   발생하지 않으므로 범위 밖(§6 관측 leg 에서 실 TUI 대조).
#
# 소비 형태: standalone 실행 스크립트 — 3경로(브릿지 py·Stop hook py·디렉티브 bash)가
#   모두 subprocess/직접 호출로 쓸 수 있다. 이번 leg 은 프리미티브+픽스처만, 배선은 별도 leg.
#
# 안전: tmux send-keys 만 수행. 라이브 세션 조작은 호출자 책임(pane 인자). 외부 발신 0.
set -euo pipefail

TMUX_BIN="${COMPOSER_CLEAR_TMUX_BIN:-tmux}"
MAX_LINES="${COMPOSER_CLEAR_MAX_LINES:-200}"
PANE=""
INTERRUPT=0
DRY_RUN=0

usage() {
  cat >&2 <<'USAGE'
Usage: composer-clear.sh --pane <tmux-target> [--interrupt] [--max-lines N]
                         [--tmux BIN] [--dry-run]
  --pane        비울 대상 tmux target (예: '=session:' 또는 pane id). 필수.
  --interrupt   맨 앞에 Escape 를 넣어 진행 중 턴을 끊는다(브릿지 interrupt 경로와 동형).
                기본 off — generating 중 오발동 시 턴을 끊지 않게.
  --max-lines   소거 반복 상한(기본 200, env COMPOSER_CLEAR_MAX_LINES).
  --tmux        tmux 실행 파일(기본 tmux, env COMPOSER_CLEAR_TMUX_BIN).
  --dry-run     키를 보내지 않고 send-keys 인자를 공백구분 1줄로 stdout 출력.
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pane) PANE="${2:-}"; shift 2 ;;
    --interrupt) INTERRUPT=1; shift ;;
    --max-lines) MAX_LINES="${2:-}"; shift 2 ;;
    --tmux) TMUX_BIN="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "composer-clear: 알 수 없는 인자 '$1'" >&2; usage ;;
  esac
done

[ -n "$PANE" ] || { echo "composer-clear: --pane 필수" >&2; usage; }
case "$MAX_LINES" in
  ''|*[!0-9]*) echo "composer-clear: --max-lines 는 양의 정수여야 함 ('$MAX_LINES')" >&2; exit 2 ;;
esac
[ "$MAX_LINES" -ge 1 ] || { echo "composer-clear: --max-lines >= 1" >&2; exit 2; }

# 키 시퀀스 조립: [Escape?] C-e (C-u BSpace)*MAX_LINES
KEYS=()
[ "$INTERRUPT" -eq 1 ] && KEYS+=("Escape")
KEYS+=("C-e")
i=0
while [ "$i" -lt "$MAX_LINES" ]; do
  KEYS+=("C-u" "BSpace")
  i=$((i + 1))
done

if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s\n' "${KEYS[*]}"
  exit 0
fi

"$TMUX_BIN" send-keys -t "$PANE" -- "${KEYS[@]}"
