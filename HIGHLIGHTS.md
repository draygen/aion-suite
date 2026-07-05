# Highlights

Notable moments worth telling — portfolio / marketing material for the AION + mcpbuilder suite.

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
