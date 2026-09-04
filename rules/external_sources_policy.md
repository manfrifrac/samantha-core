# External Sources, Fact Verification & Anti-Hallucination Policy

> **Scope**: External information retrieval standards, multi-source cross-verification, canonical URL validation, and citation rules.

---

## 🔍 Fact Verification & Anti-Hallucination Axioms

1. **Multi-Source Cross-Verification**:
   - Critical data points (e.g. government tender deadlines, regulatory compliance requirements, financial quotes) must be corroborated across at least two independent primary sources before inclusion in final deliverables.
2. **Canonical Link Validation**:
   - Extracted URLs must be programmatically verified (e.g. via HTTP HEAD/GET request confirming status 200) prior to storage.
3. **Transparent Citation**:
   - All research reports must explicitly cite the timestamp, source URL, and retrieving agent identifier.

---

## 🚫 Hallucination Prevention Directives

- Never extrapolate factual data (dates, amounts, legal articles) from memory without live web or database verification.
- If a data point is unverified or ambiguous, explicitly declare it as *"Unverified"* or *"Requires Clarification"* rather than asserting certainty.
