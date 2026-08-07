# LLM prompts — where they live

The prompt text is defined in code. **`FINALISED_LLM_PROMPTS.md` is the spec**;
this file records where each part lives and where the code deviates from it.

| Prompt part | Defined in |
| --- | --- |
| Part A — identity | `IDENTITY` in [app/graph/prompts/system.py](app/graph/prompts/system.py) |
| Part B — voice guide | `STYLE_BLOCK` in [app/graph/prompts/style.py](app/graph/prompts/style.py) |
| Part C — rules 1–14 | `RULES` in [app/graph/prompts/system.py](app/graph/prompts/system.py) |
| Stage / contact / case / RAG / collected blocks | `build_system_prompt()` in [app/graph/prompts/system.py](app/graph/prompts/system.py) |
| Part D — per-node instructions | [app/graph/prompts/templates.py](app/graph/prompts/templates.py) |
| Classifier + extractor + assault verifier | [app/graph/prompts/templates.py](app/graph/prompts/templates.py) |

To read the assembled prompt for a given turn:

```bash
.venv/Scripts/python.exe -c "from app.graph.prompts.system import build_system_prompt; print(build_system_prompt({'contact_type':'employer','history_text':'x'}))"
```

## Part B is in code, not the database

`cb_style_config` is **no longer read**. Part B is a structured guide whose
sections and ordering are part of the prompt, and storing it as a table of
one-line settings is how it drifted out of shape. Edit
[app/graph/prompts/style.py](app/graph/prompts/style.py) to change the voice.
The table can be left in place; nothing queries it.

## Deviations from the spec

- **`- WhatsApp name:`** is printed only when a name is known, rather than
  unconditionally with an empty value.
- **`- Previous enquiry:`** is sourced from the most recent `cb_tickets` row for
  the conversation (`ticket_service.last_for_conversation`), looked up only for
  `contact_type = 'employer'`. The date is rendered without the time.
- **Assault double-verification is kept**, against change #12 in the spec. The
  out-of-hours safety override in [app/api/webhook.py](app/api/webhook.py) fires
  on a deliberately wide keyword net, and without a second opinion "she was
  shouting about the salary" would message the client and page an admin. The
  verifier prompt (`ASSAULT_VERIFY_SYSTEM`) was rewritten instead: it now names
  what does and does not count, covers third-party reports, resists messages
  that claim to be emergency overrides, and errs toward `true` when a vague
  message plausibly describes harm.
- **`media_received` carries a `service_type`** of `media_received` rather than
  the `null` in the spec, because ticket creation is keyed on service type and
  the spec also asks for a ticket. The classifier is still told to return
  `null`; the value is set in code.
- **Two intents were added beyond the spec's list**, both from live failures:
  - `agency_info` — "what services do you provide" retrieves at 0.38, under the
    0.45 confidence floor, so the weak-retrieval guard was replacing the answer
    with "let me check with the team" even though Part A lists the services.
    This intent is exempt from that guard and answered from Part A via
    `AGENCY_INFO_INSTRUCTION`. The floor was **not** lowered — real content
    questions retrieve at 0.53–0.77, so lowering it would let the model
    improvise on fees and MOM rules.
  - `candidate_registration` — a helper asking for work, or an agent offering
    one, was being classified `direct_hiring` and pushed through an employer
    flow: a job seeker was asked for the employer's name and the helper's
    passport number. It now acknowledges, raises a ticket and hands over
    without collecting.
- **Emoji in the voice guide** ("An occasional 🙂 or 👍…") — the two emoji in the
  source document did not survive as valid characters; these are stand-ins.

## How a voice note and an attachment flow through

**Voice note** — transcribed in [app/api/webhook.py](app/api/webhook.py) before
anything reads the text, so the classifier, the responder and the safety
override all see plain words. Uses `openai/gpt-4o-mini-transcribe` through
OpenRouter, so no key beyond `OPENROUTER_API_KEY`. The transcript is stored as
the message body, with `media_type = 'voice'` and the audio still attached.
Rule 14 then applies: the model never mentions the voice note. If transcription
is off or fails, the message falls back to the attachment path below.

**Image / document / failed voice note** — classified `media_received`, which
routes `rag_retriever → response_generator → ticket_creator → handover_executor`:
the bot acknowledges the file under rule 13, raises a `media_received` ticket
(assignment unchanged — salesperson, else round robin) carrying the filenames,
captions and links, then hands over silently so a human can open it.
