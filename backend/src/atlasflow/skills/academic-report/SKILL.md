---
name: academic-report
description: Write evidence-grounded Chinese research reports with substantive analysis, domain-adaptive structure, and numbered academic-style citations. Use for AtlasFlow Synthesizer drafts and revisions; do not use for planning or intermediate agent decisions.
---

# Academic Report

Produce a report that can stand on its own after the research workflow is hidden.

## Writing outcome

- Start with `## 摘要`, answer the research question directly, and state the most decision-relevant findings.
- For multi-task research, develop the report rather than concatenating task summaries. Explain evidence, comparisons, causal or practical implications, uncertainty, and an actionable conclusion.
- Organize the middle sections for the subject. A travel report may use conditions, itinerary, scheduling, practical advice, and contingency plans; a market report may use current position, change, drivers, risks, and outlook.
- End with exact second-level sections `## 结论`, `## 局限`, and `## 参考文献`.
- A normal multi-task report should usually contain 1,500–3,500 Chinese characters before references when the evidence supports that depth. Simple arithmetic or a narrow factual answer should remain concise.
- Use tables only for genuine multi-item comparison or schedules. Use concrete values, dates, units, and scope where the evidence provides them.
- Do not expose task IDs, Agent names, workflow stages, prompt instructions, or internal review mechanics.

## Evidence discipline

- Separate verified facts, analysis, recommendations, and unresolved uncertainty.
- Do not add a fact merely to make the report look complete. Mark unsupported current facts as unverified and explain what evidence is missing.
- Do not turn conditional analysis into a verified conclusion.
- Preserve explicit user constraints, including place entrances and requested time ranges.
- For maps, cite only the supplied navigation references and tell the reader to open them for current distance, duration, and traffic.

## Citations

Read and follow [references/citation-contract.md](references/citation-contract.md). The supplied citation catalog is authoritative.
