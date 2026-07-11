프로젝트 가이드라인 (Project Guidelines)
기본 UX (Primary UX)
워크플로우는 bin/mini_nitpicker_daemon.py를 통한 저장 시점(save-time)의 비동기 JIT 리뷰입니다.
커밋을 차단(blocking)하는 리뷰 흐름을 제안하거나, 구축하거나, 이에 의존하지 마십시오.
- Use OOP principles to structure your code and logic for maintainability and clarity.

리뷰 우선 워크플로우 (Review-First Workflow)
코드를 대대적으로 변경하기 전에 .jemmin/logs/latest_review.txt와 .jemmin/logs/latest_review.json 파일이 존재한다면 먼저 읽으십시오.

최신 Nitpicker 리뷰를 선택적 컨텍스트가 아닌 필수 입력값으로 취급하십시오.
최신 리뷰 대상이 현재 수정 중인 파일과 일치하지 않으면, 코드를 변경하기 전에 현재 대상(active target)을 기준으로 리뷰어를 다시 실행하십시오.
최신 리뷰에 대상 파일에 대한 조치 가능한 지적 사항(actionable findings)이 포함되어 있다면, 사용자가 명시적으로 다른 지시를 내리지 않는 한 해당 지적 사항을 가장 먼저 해결하십시오.
리뷰된 코드를 변경한 후에는 관련 검증을 다시 실행하고, 가능한 경우 Nitpicker 리뷰를 갱신하십시오.

빌드 및 테스트 (Build And Test)
권장 테스트 명령어: .venv\Scripts\python.exe -m pytest tests -v

tests/conftest.py가 이미 src를 sys.path에 부트스트랩하므로, 일반적인 테스트 실행 시 PYTHONPATH=src에 의존하지 마십시오.
배치 파일이나 작업(task) 경로가 더 적절한 경우 Run_Tests.bat 또는 test: pytest VS Code task를 사용하십시오.
규칙 (Conventions)
수정 사항은 최소한으로 유지하고 활성화된 리뷰 지적 사항에만 집중하십시오.
사람이 읽을 수 있는 리뷰 출력은 .jemmin/logs/latest_review.txt에, 기계가 읽을 수 있는 출력은 .jemmin/logs/latest_review.json에 저장하십시오.
Nitpicker 출력의 리뷰 요약 및 이유는 한국어로 유지하십시오.
