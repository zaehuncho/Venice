---
auto_execution_mode: 3
---
Token and cost efficiency rules:

Work silently by default. Do not stream long explanations while editing files or running tools.

Before acting, make a short plan internally. Only tell me the plan if the task is risky, ambiguous, or needs approval.

Minimize output. Use concise status updates only when needed:
- “Working on file edits.”
- “Tests running.”
- “Found issue: [short reason].”
- “Done: [summary].”

Do not paste full files unless I ask. When changing code, summarize the changed files and the exact purpose of each change.

Prefer patch-style edits over rewriting entire files.

Do not repeat context I already gave you. Use references like “the previous issue” or “the target logic” when clear.

Use tools only when necessary. Batch related searches, reads, and edits together instead of making many small calls.

When inspecting code:
1. Search for the smallest relevant set of files.
2. Read only the relevant sections.
3. Make the smallest safe change.
4. Run only the most relevant tests first.
5. Expand scope only if the first pass fails.

When reporting results, use this format:
Done:
- Changed: [files]
- Fixed: [what changed]
- Verified: [tests/checks run]
- Remaining: [anything not done]

Do not output code blocks unless I specifically ask for the code.
Do not explain basic concepts unless I ask.
Do not narrate every step.
Prioritize completing the task over producing long commentary.