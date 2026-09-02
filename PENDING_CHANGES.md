# PENDING_CHANGES.md

Work that is agreed but not yet done. `CLAUDE.md` describes how the system **is**;
this file describes what we still intend to change.

**How to use this file**

- Add an item the moment a change is decided, not when it starts.
- Move it to the `CLAUDE.md` §11 change log when it ships, and delete it from here.
- If an item turns out to be wrong or unnecessary, delete it and say so in the commit.
- Keep it short. This is a queue, not a design doc — link to the code instead.

---

## Now

*(nothing claimed — add the item you are about to start here)*

## Next

- [ ] **Seed assignment.** `cb_round_robin_state` is empty and no `wp_chat_users` row is
      bridged to a profile, so every ticket is created unassigned.
      `scripts/seed_assignment.sql` is written but blocked on two answers from Ming Hwee:
      which consultants receive new leads, and how the 6 portal users map to profiles.
- [ ] **Extend `ASSAULT_PATTERNS` beyond English.** Claire now replies in any language,
      but the deterministic harm-keyword override is English-only — including
      `emergency_override` in `webhook.py`, the out-of-hours path.
- [ ] **Apply `scripts/ticket_lead_fk_set_null.sql`** so deleting a lead cannot fail with
      an FK error once tickets start referencing leads.
- [ ] **Rotate the webhook secret.** It is committed in plaintext in
      `.claude/settings.json` and needs scrubbing from git history.
- [ ] **Rewrite `scripts/TEST_SCRIPT.md` §E.** It still treats "admits it is a bot" as a
      failure, which is now the required behaviour.

## Later

- [ ] **Fix §1B being enforced per-table** so one phone cannot end up with both an
      employer and a candidate lead.
- [ ] **Stop a second ticket overwriting the first enquiry's lead data** — merge on
      update rather than rewriting `interest_type` / `requirement` / `summary`.
- [ ] **Make `next_lead_number` order by number, not `created_at`**, so the duplicate-key
      retry can actually converge.
- [ ] **Delete the divergent dead copies** of `EMPLOYER_LEAD_SERVICES` /
      `CANDIDATE_LEAD_SERVICES` and `_lead_name` in `ticket_creator.py`.
- [ ] **Bound the in-memory caches** that currently have no TTL: `_LOCKS`,
      `_AUTO_REPLY_VERDICTS`, `_MISSING_COLUMNS`, `_vocabularies`.

## Questions for Ming Hwee

- [ ] Which consultants should receive new leads? 18 of the 20 `sales` profiles are
      `@growwstacks.com` development accounts.
- [ ] Shirley is in the portal's *sales* department but her platform archetype is
      `admin`, which is the assault-escalation target. Should she also take normal leads?
