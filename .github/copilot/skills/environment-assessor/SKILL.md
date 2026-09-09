---
name: environment-assessor
description: Small, loop-limited agent for assessing environment health with observability tools.
---

# Environment Assessor Skill

- Start with a compact snapshot of the current window.
- Use high-level observability tools first; avoid raw PromQL/LogQL unless needed.
- Keep context small: summarize tool outputs, keep top-k low, and cap loops/tool calls.
- Stop early when the environment is healthy or when the model cannot add evidence.
- If a signal is abnormal, deepen only on the implicated family of symptoms.
- Return short structured JSON: status, services, summary, evidence, actions, source.
- Prefer deterministic fallback over long, uncertain reasoning.