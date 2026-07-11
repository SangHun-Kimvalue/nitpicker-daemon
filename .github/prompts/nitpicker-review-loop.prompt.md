---
description: "Run the Nitpicker review loop on a target until REVIEW_PASSED or a real blocker is found."
name: "Nitpicker Review Loop"
argument-hint: "Target file or review scope, plus optional test command"
agent: "agent"
model: "gemini-3.1-pro-preview"
---
Run the autonomous Nitpicker fix loop for: ${input:Target file or scope}

Workflow requirements:
- Read `.jemmin/logs/latest_review.txt` and `.jemmin/logs/latest_review.json`.
- If the latest review is stale (timestamps or target files do not match), rerun the Nitpicker command for the requested target FIRST.
- Apply the smallest, most performant code change that addresses the actual finding. Do not degrade structural integrity.
- Run the provided test/validation command (if any).
- Use OOP principles to structure your code and logic for maintainability and clarity.
- Rerun Nitpicker to verify your fix.
- Continue this loop until the result is `REVIEW_PASSED` OR you hit a hard blocker (Max 3 attempts reached, or requires architectural decisions beyond your scope).

Final response format:
1. Final review status (e.g., `[REVIEW_PASSED]`)
2. Total iterations taken
3. Files changed
4. Validation run
5. Latest Nitpicker summary
6. Remaining blocker, if any
**Note: You must write your final response and explanations entirely in Korean (한국어로 답변할 것).**