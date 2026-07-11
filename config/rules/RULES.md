# Rules — 리뷰 규칙의 계보

## 현재 규칙 — 4대 핵심 규칙 (Core Rules)

| # | 규칙 | 내용 |
|---|---|---|
| 1 | **Fail-Fast** | `except Exception: pass` 금지. 예외는 전파한다 |
| 2 | **Hot-path I/O 금지** | 루프 안 I/O·무거운 쿼리·lazy import 금지, 할당은 루프 밖으로 |
| 3 | **Strict Typing** | 모든 변수에 명시적 타입. 경계에서 타입이 흐려지면 거부 |
| 4 | **Concurrency Safety** | 공유 상태는 Lock 보호. race condition 의심 지점은 블로커 |

## 왜 4개인가 — 18계명에서의 수렴

이 규칙들은 처음부터 4개가 아니었습니다.

1. **18계명 (AutoUTube, 2026-03)** — 0.1ms 정밀 제어가 요구되는 비동기 시스템에서, LLM이 생성한 코드가
   아키텍처 규칙을 지키는지 검증하기 위해 만든 18개 조항의 시스템 프롬프트.
   Hard-Gating(필수 파일 누락 시 즉각 REJECT), 도메인 공식 검증(사격 통제 공식, O(n) 복잡도),
   RAG-lite 컨텍스트 주입과 함께 운용.
2. **ZTR (Agent 원탁 회의)** — 18계명이 Critic 에이전트의 시맨틱 리뷰 기준으로 승계됨.
   Writer-Critic 교차 리뷰에서 규칙 위반을 blocker/major/minor로 분류.
3. **Nitpicker Daemon (본 리포)** — 저장 이벤트마다 실행되는 데몬에서는 판정의 **재현성**이 생명.
   해석 여지가 있는 조항은 advisory로 내리고, 기계적으로 판정 가능한 4개 조항만 블로커로 남김.
   (규칙이 많을수록 좋은 게 아니라, **거부가 재현 가능해야** 자동화 게이트로 신뢰받는다는 교훈)
4. **cubi-skills / MM** — 코드 규칙을 넘어, 세션 간 협업 규약(역할 분리·HANDOFF·검증 의무)으로 일반화.

## 판정 원칙

- 블로커만 REJECT — 스타일·취향·추측성 지적은 `REVIEW_PASSED` + advisory
- 요약만 있는 거부 금지 — REJECT/PATCH에는 구체적 근거(라인) 또는 패치 필수
- 제공된 diff와 로컬 컨텍스트가 뒷받침하는 결함만 보고
