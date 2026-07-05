# Highlights

Notable moments worth telling — portfolio / marketing material for the AION + mcpbuilder suite.

**Track record so far: 2 for 2.** Both times the fleet's multi-agent review was pointed at
real code — first its own control hook, then a separate production alpha — it produced
concrete, actionable, verified findings a single reviewer would likely have missed.

---

## 🪞 The system reviewed its own code — and hardened itself

While building the **fleet chat-control hook** (type `fleet run draydev codex: …` into AION's
chat and it delegates to a real agent on a remote machine), I used the project's *own*
multi-agent capability — `fleet review`, which fans one prompt to several independent models
in parallel — to critique a design decision in that very hook.

**The prompt** (fanned to `codex` and `agy` on the draydev box, simultaneously):

> I built a chat command that runs shell commands on remote machines. Before executing, it
> stages a confirmation keyed by the user's client IP address… what is the single biggest
> failure mode of keying that pending confirmation by client IP instead of by authenticated
> user identity, and what one additional guardrail would you add?

**Both models independently returned the same finding:**

- **codex:** *"The biggest failure mode is confirmation hijacking/cross-talk: anyone sharing
  the same apparent client IP — such as users behind the same NAT, proxy, VPN, or office
  network — can satisfy another user's pending destructive command by replying `fleet yes`.
  Key the pending confirmation to the authenticated user/session identity, not IP, and also
  bind it to a nonce plus a normalized command/target summary…"*

- **agy:** *"The single biggest failure mode is IP sharing via NAT… require the user to
  confirm using a unique, short-lived transaction token generated during the staging phase
  (e.g., `fleet yes <token>`), ensuring only the initiator who saw the token can authorize the
  execution."*

**Why that's the interesting part:** two *different* models, running in parallel with no
knowledge of each other's answer, converged on the **same flaw** and the **same class of
fix**. When independent reviewers agree, that's high-confidence signal — much stronger than
trusting a single pass. The design really was keyed by client IP (a shortcut I took to avoid
threading identity through the dispatch layer), and the panel caught it.

**The fix** (commit `1b86703`): the confirmation is now keyed to the **authenticated
username** and gated by a **one-time token** shown at staging (`fleet yes <token>`), compared
with `secrets.compare_digest` and consumed on use — so only the person who staged an action
can confirm that exact action, once. Verified end-to-end: wrong/missing tokens are rejected
(without discarding the staged action), and confirming from a brand-new session as the same
user still works — proving it's bound to identity, not IP or session.

**The one-liner:** *A built-in panel of independent AI models reviewed the system's own
control hook, caught a real security flaw a single pass would have missed, agreed strongly
enough to act on it — and the fix shipped the same session.*

---

## 🔍 It security-reviewed a real production alpha — and shipped the fixes

The second proof came on a *different, real* project: **Draygen Secure Transfer (DST)**, a
secure enterprise file-transfer app running live at `dst.drayhub.org` on the same EC2 box the
fleet reaches. The task: "can this setup actually review my alpha?" Three passes, all through
the same AION + fleet + MCP stack:

1. **Black-box scan** of the live site (an authorized target in AION's allowlist) — port scan
   of the EC2 host, TLS/header inspection, live CORS and dev-backdoor probes.
2. **Multi-agent code review** — the security-critical Spring files fanned to `codex` + `agy`
   in parallel.
3. Manual verification of every candidate finding against the source.

**What it produced:**

- **Confirmed a genuinely strong posture** (not just hand-waving): clean attack surface
  (only 22/80/443 open), no live dev backdoor (`mock-login` → 403 in prod), CORS pinned to the
  real origin (hostile-`Origin` preflight rejected), actuator not exposed, and an
  **IDOR-safe token download flow** with the password gate enforced on the actual byte stream.
- **Found real hardening gaps** — `codex` independently flagged the same CORS-with-credentials
  fragility and recipient-token concerns the manual review did. Four findings; **three fixed
  and compile-verified the same session**, one documented for follow-up. Full write-up lives in
  the DST repo at `docs/SECURITY_REVIEW_2026-07-05.md`.
- **A candid tooling signal:** `agy` *refused* the security task ("cannot identify
  vulnerabilities") — a safety-filter false-positive — while `codex` delivered. That's exactly
  why a panel beats a single reviewer: one model went dark and the review still landed.

**Why it matters:** the first highlight could be dismissed as a system reviewing itself. This
one wasn't — it was pointed at an independent, live, security-sensitive product and returned a
review good enough to change the code. That's the whole value proposition: **a small local
setup that turns "get a second (and third) expert opinion on real code" into a one-line
command — and it keeps finding things.**

**The one-liner:** *Pointed at a live secure-file-transfer alpha, the multi-agent fleet
confirmed what was already solid, caught what wasn't, and the fixes shipped — the second time
in a row it earned its keep.*
