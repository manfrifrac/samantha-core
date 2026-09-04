# Inter-Agent Delegation & Mandate Prompt Engineering Guide

> **Scope**: Structural guidelines for authoring actionable, unambiguous mandates and system prompts for autonomous agents and ephemeral executors.

---

## ✍️ The 8 Golden Rules of Inter-Agent Mandates

Inter-agent communication is not informal conversation—it is an executable technical specification.

### 1. Concrete Deliverables
- Specify the exact output path, file format, and structure (e.g. *"Draft response in `/tmp/docs/reply.md` with 3 sections and test results"*, rather than *"Handle the invoice"*). Vague prompts yield vague executions.

### 2. Context & Rationale ("The Why")
- Provide the strategic objective behind the task. Understanding the context allows the recipient agent to exercise judgment on edge cases without stalling.

### 3. Provide Known Context (Anti-Rediscovery)
- Include all known facts (PIDs, listening ports, file paths, candidate URLs, prior error traces) directly in the brief. Never force an agent to spend thousands of tokens scanning the disk for facts already known to the sender.

### 4. Explicit Negative Constraints
- Clearly declare what the agent **MUST NOT** do (e.g., *"Read-only inspection, no service restarts"*, *"Markdown documentation only, zero database record creation"*).

### 5. Structured Steps for Complex Tasks
- For workflows exceeding 2–3 actions, use a numbered list. Structured steps are easier to execute sequentially and audit empirically.

### 6. Concrete "Definition of Done"
- Define explicit completion criteria (e.g., *"HTTP 200 response on port 9224"*, *"Unit test suite reports 3/3 PASS"*, *"Delivery confirmation recorded in a2a_ack"*).

### 7. Clarify Ambiguity (Ask, Do Not Guess)
- If a directive has multiple plausible interpretations, ask for clarification (using `[QUESTION_OPTIONS:]` for humans or A2A for peer agents) rather than executing on an unverified assumption.

### 8. Prefer Positive Directives
- Instructing what **TO DO** is more effective than solely listing prohibitions. Provide the approved pattern alongside the constraint.
