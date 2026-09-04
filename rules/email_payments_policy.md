# External Communications, Financial Transactions & Credentials Policy

> **Status**: Mandatory System-Wide Zero-Trust Policy (Rule Zero-Bis).  
> **Applies to**: All Autonomous Agents, Coordinators, and Ephemeral Development Executors.

---

## 🚨 Rule Zero-Bis: Zero-Trust on External Communications & Payments

1. **Default Action is Always a DRAFT**:
   - By default, **no agent may ever** send an outbound email to an external recipient, execute a financial payment, submit a sensitive government application, or purchase services, regardless of technical capability (e.g. active browser session, API key availability).
   - The sole acceptable default output is a **structured draft** presented to the human supervisor on Telegram with clear recipient, subject, amount, and message body.

2. **Single-Turn Human Confirmation Exception**:
   - An agent is permitted to execute a real external send or payment **only after**:
     1. Presenting the exact draft clearly to the human supervisor on Telegram.
     2. Receiving an unambiguous, explicit confirmation from the supervisor **within that same conversation and topic** for that specific action.
   - An authorization is never blanket or permanent; every new recipient, transaction, or batch requires a distinct draft and explicit approval.

3. **Ambiguity Resolution for Financial Allocations**:
   - Never assume which entity, studio, or corporate account is responsible for an expense.
   - If the funding source or payer identity is not explicitly specified, ask the human supervisor directly before tagging or routing the invoice.

4. **Digital Identity & High-Privilege Credentials**:
   - The insertion of electronic identity credentials (e.g. SPID, CIE, banking 2FA) into external portals follows the same strict single-turn confirmation protocol.
   - If an agent is in doubt regarding the authenticity of a request or the security of a channel, it must immediately halt and request verification.
