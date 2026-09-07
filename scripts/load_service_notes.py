"""Load the agency's transfer + passport-renewal service notes into the KB.

Ming Hwee sent these timings on 2026-09-03, and the service process +
timeline table (transfer, passport renewal, new hiring, direct hiring) and the
FDW passport renewal process flow (documents, embassy contract route, runner) on
2026-09-07. Until they are IN the knowledge
base the bot cannot say them: every figure is checked against the retrieved
records by guards.ungrounded_figures, so an unretrieved "6 to 8 weeks" is
binned and the client gets "I'll check with the team" instead — which is
exactly what was happening on passport and transfer questions.

Rewritten as client-facing Q&A. The source text was staff-facing ("you may
advise employers as follows"); that phrasing must not reach the KB, because
whatever is in the records is what the model quotes.

Idempotent: a row with the same question + service_type is skipped, so this can
be re-run safely. Read-then-write — it reads a live row first to confirm the
column set rather than trusting this file's idea of the schema (CLAUDE.md §0.5).

    python scripts/load_service_notes.py --dry-run    # show what would be written
    python scripts/load_service_notes.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.supabase import db  # noqa: E402
from app.services.rag import KB_TABLE, embed_query  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logger = logging.getLogger("load_service_notes")

# Must not match rag._INTERNAL_SOURCES (MHOS / for Vendor / Blueprint /
# Hiring Pipelines Brief / version control) or every row is dropped from
# retrieval without an error.
SOURCE_DOCUMENT = "Ming Hwee Service Notes"

# nationality is varchar(5) and the CHECK constraint
# cb_knowledge_base_updated_nationality_check requires UPPERCASE codes —
# 'PH'/'ID'/'MM' — alongside lowercase 'all'. Confirmed against the live table
# (all 253, PH 12, ID 4, MM 3) after a lowercase 'ph' was rejected with 23514.
# nationality_code() emits the same uppercase codes and the match function
# compares them exactly, so these rows are reachable by a nationality-filtered
# search. Do not "normalise" these to lowercase: the constraint will reject it.
# contact_type vocabulary is all/candidate/employer. Both filters are inclusive
# of their catch-all bucket, so 'all' is reachable from every query.
ROWS: list[dict[str, Any]] = [
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer — process and timing",
        "question": "How long does a transfer take and what is the process?",
        "answer": (
            "A transfer is subject to MOM approval, which usually takes 1 to 3 working "
            "days unless MOM asks for additional documents to be uploaded. Once MOM "
            "approves the transfer we purchase the required insurance, and after the "
            "insurance is transmitted the helper can start work with the new employer "
            "the following day."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal — timing by nationality",
        "question": "How long does a passport renewal take for a helper?",
        "answer": (
            "It depends on the helper's nationality and her embassy. For a Filipino "
            "helper it takes roughly 6 to 8 weeks from the embassy appointment; for an "
            "Indonesian helper roughly 3 working days; for a Myanmar helper the "
            "appointment itself is usually done within a day, though getting an "
            "appointment can take weeks or months. These are estimates and vary with "
            "appointment availability, document verification and the embassy's own "
            "requirements."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "PH",
        "section_heading": "Passport renewal — Filipino helper",
        "question": "How long does passport renewal take for a Filipino helper?",
        "answer": (
            "Roughly 6 to 8 weeks from the date of the appointment at the Embassy of "
            "the Republic of the Philippines in Singapore. The passport is processed "
            "and printed in the Philippines, shipped back, and then made ready for "
            "collection in Singapore. This is an estimate and can vary with appointment "
            "availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "ID",
        "section_heading": "Passport renewal — Indonesian helper",
        "question": "How long does passport renewal take for an Indonesian helper?",
        "answer": (
            "Roughly 3 working days. The Indonesian Embassy in Singapore requires an "
            "online appointment, and passport services are handled during weekday "
            "operating hours. This is an estimate and can vary with appointment "
            "availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "MM",
        "section_heading": "Passport renewal — Myanmar helper",
        "question": "How long does passport renewal take for a Myanmar helper?",
        "answer": (
            "The in-person appointment is generally completed within a day once it is "
            "scheduled, but securing an appointment can take weeks or months depending "
            "on availability. This is an estimate and can vary with document "
            "verification."
        ),
    },
    # --- 2026-09-07: the agency's service process + timeline table ---------
    #
    # Additive only. The five rows above are untouched - the script skips on
    # question + service_type, so re-running it will not rewrite them.
    #
    # What this adds is the PROCESS half. Until now the KB held passport
    # timings with no steps, and nothing at all for new hiring or direct
    # hiring, so "what is the process?" fell under the soft floor and the
    # client got the holding line. It has bitten in testing three times.
    #
    # Written from the client's side of the desk, not ours. The failure this
    # avoids is on record: a "what's the process" question once retrieved the
    # internal pipeline brief and the bot replied "The process involves three
    # main stages: first, we capture your requirements and match you with
    # suitable candidates" - our own workflow, described to the person it is
    # being run on. So these say what HAPPENS and what the client will be asked
    # to do, never what our internal stages are called.
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer - the steps",
        "question": "What are the steps to transfer a helper to a new employer?",
        "answer": (
            "First the transfer application is submitted to MOM and we wait for their "
            "approval, which usually takes 1 to 3 working days. MOM sometimes asks for "
            "extra documents to be uploaded, and that adds to the wait. Once MOM "
            "approves it the required insurance is purchased, and once the insurance "
            "is transmitted the helper can start work with her new employer the "
            "following day."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - the steps",
        "question": "What is the process for renewing my helper's passport?",
        "answer": (
            "It runs through her own country's embassy in Singapore, so the steps "
            "depend on her nationality. In every case an appointment at the embassy "
            "has to be secured first, the helper attends it in person, the passport is "
            "then processed, and it is made ready for collection in Singapore. What "
            "differs is how long each part takes and where the passport is actually "
            "printed. Tell us her nationality and we can be specific."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "PH",
        "section_heading": "Passport renewal - Filipino helper, the steps",
        "question": "What is the process for renewing a Filipino helper's passport?",
        "answer": (
            "An appointment is made at the Embassy of the Republic of the Philippines "
            "in Singapore and the helper attends it in person. The passport is then "
            "processed and printed in the Philippines, shipped back to Singapore, and "
            "made ready for collection here. From the date of the embassy appointment "
            "it usually takes about 6 to 8 weeks, though that can vary with "
            "appointment availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "ID",
        "section_heading": "Passport renewal - Indonesian helper, the steps",
        "question": "What is the process for renewing an Indonesian helper's passport?",
        "answer": (
            "It is processed through the Indonesian embassy in Singapore. An online "
            "appointment is booked, the helper attends in person during weekday "
            "operating hours, and the renewal is processed from there - usually about "
            "3 working days. That can vary with appointment availability and document "
            "verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "MM",
        "section_heading": "Passport renewal - Myanmar helper, the steps",
        "question": "What is the process for renewing a Myanmar helper's passport?",
        "answer": (
            "There are three parts: securing an appointment, attending it in person, "
            "and the renewal being processed. Once the appointment is secured the "
            "in-person part is generally completed within a day. The waiting time for "
            "the appointment itself is the long part - it can run to weeks or months "
            "depending on availability, so it is worth starting early."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - what to expect",
        "question": "What is the process for hiring a new helper?",
        "answer": (
            "We start by going through what you need. That covers who is at home and "
            "the ages of any children or elderly family members, the type of home and "
            "how many bedrooms and bathrooms there are, whether you have pets or any "
            "dietary restrictions, and what her main and secondary duties would be. "
            "Then we take your preferences - which nationality you would like, and "
            "whether you are open to a first-timer or would rather someone who has "
            "worked in Singapore before, worked abroad, or is a transfer helper "
            "already here. We also ask when you need her to start. With all of that we "
            "recommend the helpers who genuinely suit your household, rather than "
            "sending you a stack of profiles to sift through."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - start date",
        "question": "How long does it take to hire a new helper?",
        "answer": (
            "There is no single answer, because it turns on your requirements and on "
            "which helper you choose. What we work to is your own date - so the "
            "question we ask is when you need her to start, and we plan back from "
            "that. A consultant will confirm the timeline once your requirements are "
            "in and we know which helper you are going for."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - what to expect",
        "question": "What is the process for a direct hire?",
        "answer": (
            "A direct hire is one where you have already found the helper yourself and "
            "want us to process her. We take her full name and contact number, confirm "
            "where she is at the moment - here in Singapore, back in her home country, "
            "or working in another country - along with her nationality, whether she "
            "is currently employed, and when she would be free to start. If she is "
            "employed overseas we also check whether she has a notice period to serve "
            "or any clearance to obtain from her current employer before she can "
            "leave. Once we have that we can set out the procedure, the documents "
            "required, the likely timeframe and the costs."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - timing",
        "question": "How long does a direct hire take?",
        "answer": (
            "There is no fixed timeline for a direct hire. It depends on where the "
            "helper is, her nationality, whether she is currently employed, when she "
            "is available, and whether she has a notice period or clearance to "
            "complete before she can leave her current job. Once we have those details "
            "we can give you a proper estimate rather than a guess."
        ),
    },
    # --- 2026-09-07: the FDW passport renewal process flow -----------------
    #
    # The document list. This is the gap that had been open since 2026-09-04
    # and was measured at **0.000** the same day the process rows went in:
    # "what documents are needed", asked under service=passport_renewal with
    # nationality=PH, matched NOTHING in the whole knowledge base, so a client
    # asking the single most practical question about a renewal got the holding
    # line.
    #
    # The branch that matters is the embassy contract, and it is decided by
    # nationality, not by asking:
    #
    #     Philippines  -> holds an embassy contract
    #     Indonesia    -> holds an embassy contract
    #     Myanmar      -> does NOT, so three more forms are signed first
    #
    # `passport_renewal` already collects `nationality`, so the route is
    # derivable and no new question is added for it. Asking an employer whether
    # their helper holds an embassy contract would be exactly the interrogation
    # the agency objected to on 2026-09-04.
    #
    # NO DURATIONS IN ANY ROW BELOW. The source flow is explicit that it
    # provides the process and NOT a processing time, and warns against
    # inventing one from it. The timings the bot may quote are the separate
    # 2026-09-03 rows above, which came from the agency's own timing table.
    # If a figure appears here that is not in those rows, guards.ungrounded_
    # figures will bin the whole reply and the client gets a holding line -
    # so adding a plausible-sounding number here makes the bot WORSE, not
    # more helpful.
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - documents required",
        "question": "What documents are needed to renew my helper's passport?",
        "answer": (
            "It depends on whether she holds a valid embassy contract - a Filipino or "
            "Indonesian helper normally does, a Myanmar helper does not. If she holds "
            "one, what goes to the embassy is the Application for Passport Renewal "
            "Form she completes, her embassy contract, a copy of your IC, a copy of "
            "her passport, her Singapore work pass and her work permit. If she does "
            "not hold one, three further forms have to be signed first - the "
            "Undertaking of Employer Form, the Standard Employment Contract and the "
            "Information Sheet of Employer - and those go in alongside the same "
            "application form and copies."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - no embassy contract",
        "question": "What extra forms are needed if my helper has no embassy contract?",
        "answer": (
            "Three, and they are signed before anything goes to the embassy. The "
            "Undertaking of Employer Form is signed by you. The Standard Employment "
            "Contract and the Information Sheet of Employer are each signed by both "
            "you and your helper. Those three then go to the embassy together with "
            "the Application for Passport Renewal Form, a copy of your IC, a copy of "
            "her passport, her Singapore work pass and her work permit. This is the "
            "route a Myanmar helper takes, since Myanmar helpers do not hold an "
            "embassy contract."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - embassy contract by nationality",
        "question": "Does my helper have an embassy contract?",
        "answer": (
            "Filipino and Indonesian helpers hold an embassy contract. Myanmar "
            "helpers do not. It matters because it decides the paperwork: with a "
            "contract the renewal application goes in with the contract itself and "
            "the usual copies, and without one the Undertaking of Employer Form, the "
            "Standard Employment Contract and the Information Sheet of Employer have "
            "to be signed first."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - what the employer does",
        "question": "What do I need to do as the employer for my helper's passport renewal?",
        "answer": (
            "Mostly signing and providing copies. We need a copy of your IC. If your "
            "helper does not hold an embassy contract, which is the case for a "
            "Myanmar helper, you also sign the Undertaking of Employer Form, and you "
            "and she both sign the Standard Employment Contract and the Information "
            "Sheet of Employer. The embassy trip itself is handled by a runner: for "
            "an Indonesian or Myanmar helper he collects her from your home and "
            "brings her back, and a Filipino helper reports to the embassy herself "
            "and meets him there. We let you know once the renewal is done."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - going to the embassy",
        "question": "Does someone go with my helper to the embassy?",
        "answer": (
            "Yes, a runner assists her. For an Indonesian or a Myanmar helper the "
            "runner collects her from your home, takes her to the embassy and brings "
            "her back afterwards. A Filipino helper has to report to the embassy in "
            "person herself, and meets the runner there. Either way the runner tells "
            "us as soon as it is done and we update you on the status."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "PH",
        "section_heading": "Passport renewal - Filipino helper, documents and visit",
        "question": "What documents does a Filipino helper need for passport renewal, and how does the embassy visit work?",
        "answer": (
            "She holds an embassy contract, so the paperwork is the Application for "
            "Passport Renewal Form she completes, her embassy contract, a copy of "
            "your IC, a copy of her passport, her Singapore work pass and her work "
            "permit, all submitted to the Philippine embassy. She is required to "
            "report to the embassy in person - she makes her own way there and meets "
            "our runner at the embassy, who assists her through it. Afterwards the "
            "runner sees her back to your home where that applies, tells us it is "
            "complete, and we update you."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "ID",
        "section_heading": "Passport renewal - Indonesian helper, documents and visit",
        "question": "What documents does an Indonesian helper need for passport renewal, and how does the embassy visit work?",
        "answer": (
            "She holds an embassy contract, so the paperwork is the Application for "
            "Passport Renewal Form she completes, her embassy contract, a copy of "
            "your IC, a copy of her passport, her Singapore work pass and her work "
            "permit, all submitted to the Indonesian embassy. Our runner collects her "
            "from your home, takes her to the embassy for the renewal and brings her "
            "back afterwards, so she is accompanied both ways. The runner then tells "
            "us it is complete and we update you."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "MM",
        "section_heading": "Passport renewal - Myanmar helper, documents and visit",
        "question": "What documents does a Myanmar helper need for passport renewal, and how does the embassy visit work?",
        "answer": (
            "Myanmar helpers do not hold an embassy contract, so three forms are "
            "signed first: the Undertaking of Employer Form, which you sign, and the "
            "Standard Employment Contract and Information Sheet of Employer, which "
            "you and she both sign. Those go to the embassy along with the "
            "Application for Passport Renewal Form, a copy of your IC, a copy of her "
            "passport, her Singapore work pass and her work permit. Our runner "
            "collects her from your home, takes her to the embassy and brings her "
            "back afterwards. The runner then tells us it is complete and we update "
            "you."
        ),
    },
    # --- 2026-09-08: the new-hiring document checklist and the full process --
    #
    # The agency sent both on 2026-09-08. Two separate gaps:
    #
    #   1. DOCUMENTS. Nothing in the KB told an employer what they personally
    #      have to produce to hire a helper, so "what documents do I need"
    #      under service=new_hiring had nothing to retrieve. It is the second
    #      most practical question a first-time employer asks, after the cost.
    #
    #   2. THE PROCESS END TO END. The 2026-09-07 rows describe how we take
    #      requirements and match helpers - stage one of five. Everything after
    #      the client picks somebody (MOM, the IPA, the embassy, insurance and
    #      the bond, travel, arrival) was absent, so "what happens next" after
    #      a confirmed choice retrieved the requirements answer again.
    #
    # REWRITTEN FROM THE SOURCE, NOT COPIED. The agency's flow is written for
    # staff: it names an internal owner for every phase, the internal system,
    # the page count of the MOM form and a retention target. None of that may
    # reach the KB, because whatever is in the records is what the model quotes
    # back - the recorded failure is a "what's the process" question retrieving
    # the internal pipeline brief and the bot reciting our own workflow to the
    # person it is being run on. Every row below says what HAPPENS and what the
    # CLIENT does, and names no internal team or stage.
    #
    # FIGURES: the only one carried over is the $5,000 security bond, which
    # CLAUDE.md records as deliberately quotable. The MOM application fee is
    # described WITHOUT its amount on purpose - a new hire's costs do not reach
    # a client before a salesperson has spoken to them (client instruction,
    # 2026-09-04), and guards.quotes_hiring_package_cost would swap the reply
    # for the deferral line anyway. No duration appears anywhere below: the
    # source gives none for new hiring, and inventing one gets the whole reply
    # binned by ungrounded_figures.
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - documents from the employer",
        "question": "What documents do I need to provide to hire a helper?",
        "answer": (
            "From you we need a copy of your NRIC or identity document, and proof of "
            "your income - either your Income Tax Assessment or a declaration of your "
            "monthly income. If you are a foreigner working here, we need your "
            "Employment Pass or S Pass and a copy of your passport, and if you started "
            "that job recently, a letter from your company confirming your position, "
            "salary and appointment date, together with your tenancy agreement. If this "
            "would be an additional helper rather than your first, we also need identity "
            "documents for the children or elderly family members she would be caring "
            "for, as proof of the care need. Everything else is paperwork we prepare for "
            "you to sign."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - documents we prepare",
        "question": "What documents does Ming Hwee prepare for me to sign?",
        "answer": (
            "Most of the paperwork is ours to prepare and yours only to sign: the "
            "Service Agreement, the Service and Fee Schedule, the agency fee package "
            "form, an Authorisation Form that lets us handle your work pass "
            "transactions with MOM, an Employer Particulars form, the last page of the "
            "helper's biodata, and the Job Offer Form, which you and the helper both "
            "sign. If you are employing a helper for the first time there is a levy "
            "GIRO form as well. If she has worked in Singapore before, we prepare her "
            "employment history form too. We tell you what to sign and when, so nothing "
            "is left for you to work out."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - documents from the helper",
        "question": "What documents are needed from the helper herself?",
        "answer": (
            "A copy of her passport, her medical report and her school certificate. We "
            "collect those through our overseas partner rather than asking you to chase "
            "them. If she has worked in Singapore before, her employment history is "
            "needed as well. Her medical fitness has to be confirmed before the "
            "application goes to MOM."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - foreign employer",
        "question": "I am a foreigner working in Singapore, what do I need to hire a helper?",
        "answer": (
            "We need your Employment Pass or S Pass together with a copy of your "
            "passport. If you have only recently started that job we also need a letter "
            "from your company stating your position, your salary and your date of "
            "appointment, and a copy of your tenancy agreement. The usual employer "
            "documents apply alongside those - proof of your monthly income, and your "
            "identity document."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - additional helper",
        "question": "I already employ a helper and want a second one, what extra documents are needed?",
        "answer": (
            "For an additional helper we need evidence that the care need is real, which "
            "means identity documents for the children or elderly family members she "
            "would be looking after. Everything else is the same as a first hire - your "
            "identity document, proof of your monthly income, and the forms we prepare "
            "for you to sign. A consultant will confirm whether your household qualifies "
            "for a second helper before anything is submitted."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - the five stages",
        "question": "What are the stages of hiring a helper from start to finish?",
        "answer": (
            "There are five. First we go through your household and what you need, and "
            "shortlist helpers who genuinely suit it. Second, once you have chosen one "
            "and confirmed her, we apply to MOM for her work pass and wait for the "
            "In-Principle Approval. Third, her papers go through her own country's "
            "embassy and she completes her medical. Fourth, we arrange her insurance and "
            "her security bond, then book her flight once you and she have both "
            "confirmed. Fifth, she arrives, we take her through her settling-in "
            "formalities and hand her over to you, and we check in with you both "
            "afterwards. Tell us which stage you would like more detail on."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - how matching works",
        "question": "How do you match me with a helper?",
        "answer": (
            "We start with your household - who lives there, the ages of any children or "
            "elderly family members, your home and how it is laid out, your budget, and "
            "any language or cultural preferences. From that we shortlist a handful of "
            "helpers who fit, usually three to five, and send you their profiles with "
            "their experience, their skills, a video introduction and references. You "
            "can interview the ones you like by video or phone, and we arrange "
            "translation where it is needed. When you settle on one, confirming her is "
            "what starts the application."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - interviewing candidates",
        "question": "Can I interview the helper before I decide?",
        "answer": (
            "Yes. We arrange a video or phone interview with any helper on your "
            "shortlist and provide translation if you need it. You are under no "
            "obligation to take anyone you interview - if none of them feel right we go "
            "back and shortlist again. Once you do choose, confirming her is what starts "
            "the work pass application."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - after you confirm a helper",
        "question": "What happens after I confirm the helper I want?",
        "answer": (
            "This part runs strictly in order and each step waits for the one before it. "
            "You set up your Singpass and your MOM employer account, then authorise us "
            "through Singpass to apply for her on your behalf. If this is your first "
            "helper, you complete the Employers' Orientation Programme. We prepare your "
            "document set for signing and collect her passport copy, medical report and "
            "school certificate. Her medical fitness is confirmed, and we submit the "
            "application to MOM and pay the application fee. When MOM issues the "
            "In-Principle Approval we send it on, and that is what lets her embassy "
            "paperwork begin."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - what the employer does",
        "question": "What do I need to do myself when hiring a helper?",
        "answer": (
            "Less than most people expect. You tell us what your household needs and "
            "choose from the helpers we shortlist. You set up your Singpass and MOM "
            "employer account and authorise us to act for you, and if this is your first "
            "helper you complete the Employers' Orientation Programme online. You sign "
            "the document set we prepare, and give us your identity document and proof "
            "of your income. You confirm when you are available to receive her so her "
            "flight can be booked, and on handover day you sign the orientation "
            "checklist. We handle the submissions, the embassy paperwork, the insurance "
            "and the bond."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - the IPA",
        "question": "What is an IPA?",
        "answer": (
            "The In-Principle Approval is MOM's approval in principle of your helper's "
            "work permit application. It is issued after we submit the application. It "
            "has to be signed by both you and your helper and uploaded back to MOM "
            "before the Work Permit itself can be issued, and it is also what allows her "
            "embassy paperwork and her travel to be arranged - so it is the point the "
            "rest of the process waits on."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - the orientation programme",
        "question": "Do I need to attend a course before hiring a helper?",
        "answer": (
            "Only if this is your first time employing a helper. In that case you "
            "complete the Employers' Orientation Programme, which is done online at "
            "eop.com.sg, and it has to be finished before your application can go to "
            "MOM. If you have employed a helper before, you skip it. We will tell you "
            "which applies to you and remind you when it is your turn in the sequence."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - the embassy stage",
        "question": "What happens at the embassy stage after the IPA is issued?",
        "answer": (
            "It depends on her nationality, because each embassy runs it differently. A "
            "Filipino helper signs her contract, it is submitted to her embassy online, "
            "and she attends an accredited clinic for a fit-to-fly medical. An "
            "Indonesian helper's job order goes through her embassy's portal, and once "
            "it is approved you sign the employment contract copies that go in with it. "
            "For a Myanmar helper the approval papers are signed by you and sent to our "
            "partner there for her signature and her earliest departure date, along with "
            "the security bond form her immigration clearance needs. Tell us her "
            "nationality and we can be specific."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "PH",
        "section_heading": "New hiring - Filipino helper after the IPA",
        "question": "What happens after the IPA for a Filipino helper?",
        "answer": (
            "Her employment contract is printed, she signs it, and it is submitted to "
            "her embassy through the Philippine online service. Once it comes back "
            "stamped we hold it on file. She also attends a clinic accredited by the "
            "Philippine Department of Health for her fit-to-fly medical, and that "
            "certificate goes in with her papers. When both are done her travel can be "
            "arranged."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "ID",
        "section_heading": "New hiring - Indonesian helper after the IPA",
        "question": "What happens after the IPA for an Indonesian helper?",
        "answer": (
            "A job order is submitted through the Indonesian embassy's portal. When it "
            "is approved an employment contract is generated, and you sign three "
            "original copies of it along with the IPA form. Those go to the embassy "
            "together with a copy of your identity document and a copy of her passport, "
            "taken over by our runner, and the approved contract comes back to us. Her "
            "travel can be arranged once that is done."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "MM",
        "section_heading": "New hiring - Myanmar helper after the IPA",
        "question": "What happens after the IPA for a Myanmar helper?",
        "answer": (
            "We send you the IPA form to sign, then pass it to our partner in Myanmar "
            "for your helper to sign and to confirm the earliest date she can travel. We "
            "also obtain the security bond transmission form from MOM and send it "
            "across, because her immigration clearance cannot be completed without it. "
            "Once both are back her travel can be arranged."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - before she flies",
        "question": "What happens before my helper flies to Singapore?",
        "answer": (
            "Four things. The IPA has to be signed by both you and her and uploaded to "
            "MOM, which is what allows the Work Permit to be issued. We arrange her "
            "insurance and the security bond of $5,000 that MOM requires, and confirm "
            "the bond has been transmitted. We check when you are available to receive "
            "her before any ticket is issued, and book the flight only once you and she "
            "have both confirmed in writing. Then we arrange her airport pick-up and "
            "prepare your handover set - the employment contract, the salary schedule, "
            "the rest day form, the safety agreement, and her do's and don'ts."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - arrival and settling in",
        "question": "What happens when my helper arrives in Singapore?",
        "answer": (
            "She is met at the airport and taken through her arrival formalities - her "
            "Settling-In Programme, her medical examination and her fingerprinting - and "
            "we register her for the programme within the window MOM allows. On handover "
            "day you complete an orientation checklist together with her and with us, "
            "and all three sign it. The placement fee is settled that day, by PayNow, "
            "cash or cheque, and a consultant will have taken you through the figures "
            "well before then. Her Work Permit card comes through afterwards and we pass "
            "it to you. We call you and her within a week of her arrival to check how "
            "she is settling in."
        ),
    },
    # --- 2026-09-08: direct hire, documents and the full process ------------
    #
    # Same treatment as the new-hiring rows above and the same two gaps: no
    # document checklist at all, and a process that stopped at "we take her
    # details". Rewritten from the client's side - the source routes a ticket
    # to sales/admin, warns staff that a filing error costs two weeks, and
    # names the internal owner of each step. None of that is the client's
    # business and none of it may enter the KB.
    #
    # THE BRANCH IS WHERE SHE IS, and unlike new hiring it changes the
    # TIMELINE, not just the paperwork: a helper already in Singapore on a
    # valid permit skips the embassy and the flight entirely (2 to 3 weeks)
    # while one overseas goes through both (4 to 6 weeks). `direct_hiring`
    # already collects `helper_location`, so the route is derivable and no new
    # question was added - and info_collector._LOCATION_DEPENDENT stops the
    # model committing to one route before it has been told which applies.
    # Answering the wrong one is a delivery date the client will plan around.
    #
    # NO INSURANCE MINIMUMS IN ANY ROW BELOW, deliberately. The source states
    # medical insurance at a minimum of $15,000/yr, and that is the figure MOM
    # used BEFORE October 2023; the current minimum is $60,000/yr, with
    # $15,000 surviving as the co-payment threshold. Ming Hwee's own Client
    # Service Agreement says $60,000 and one FAQ row still says $15,000, so
    # the KB already disagrees with itself three ways. Writing either number
    # here would pick a side in a legal minimum on the agency's behalf. The
    # rows say "MOM's minimum coverage" and defer the figure to a consultant
    # until Ming Hwee confirms it. See CLAUDE.md section 9.
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - documents from the employer",
        "question": "What documents do I need to provide for a direct hire?",
        "answer": (
            "From you we need a copy of your NRIC or identity document and proof of your "
            "income - either your Income Tax Assessment or a declaration of your monthly "
            "income. If you are a foreigner working here, that becomes your Employment "
            "Pass or S Pass with a copy of your passport, or a letter from your company "
            "together with your tenancy agreement. From the helper we need a copy of her "
            "passport, her medical report and her school certificate. If she is already "
            "in Singapore on a work permit we also need her current permit details. "
            "Everything else is paperwork we prepare for you to sign."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - documents we prepare",
        "question": "What documents does Ming Hwee prepare for a direct hire?",
        "answer": (
            "The Service Agreement and the Service and Fee Schedule at the direct-hire "
            "rate, the agency fee package form, an Authorisation Form so we can transact "
            "with MOM on your behalf, an Employer Particulars form, the last page of the "
            "helper's biodata, and the Job Offer Form. If you are employing a helper for "
            "the first time there is a levy GIRO form as well, and if she has worked in "
            "Singapore before we prepare her employment history form. We tell you what "
            "to sign and when."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - documents from the helper",
        "question": "What documents are needed from the helper for a direct hire?",
        "answer": (
            "A copy of her passport, her medical report and her school certificate. If "
            "she is already in Singapore on a valid work permit we also need her "
            "existing permit details, because her application then follows a different "
            "route from a helper coming in from overseas. Her medical fitness has to be "
            "confirmed before the work permit application is filed."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - helper already in Singapore",
        "question": "The helper I want is already in Singapore, what do you need from her?",
        "answer": (
            "Her current work permit details, on top of the usual copy of her passport, "
            "her medical report and her school certificate. It matters because a helper "
            "already here on a valid permit does not go through her embassy and does not "
            "need to travel, so her application runs the shorter of the two routes."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - the steps",
        "question": "What is the direct hire process step by step?",
        "answer": (
            "There are six steps. First we confirm which helper you have in mind and "
            "where she is, since a helper already in Singapore on a valid permit follows "
            "a shorter route than one coming from overseas. Second, you authorise us "
            "through Singpass to deal with MOM on your behalf. Third, we collect your "
            "documents, prepare the set for you to sign, and confirm she is medically "
            "fit. Fourth, we file the work permit application with MOM. Fifth, we "
            "arrange the security bond and the insurance MOM requires, and coordinate "
            "her medical examination. Sixth, once approval is issued she either goes "
            "through her embassy and travels in, or if she is already here we go "
            "straight to her work permit. Tell us which step you would like more detail "
            "on."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - after you confirm the helper",
        "question": "What happens after I tell you which helper I want to direct hire?",
        "answer": (
            "You authorise us through Singpass to transact with MOM on your behalf. We "
            "collect your documents, prepare the set for you to sign, and confirm your "
            "helper is medically fit. Then we file the work permit application with MOM "
            "and arrange the security bond and the insurance MOM requires. We keep you "
            "posted at each point rather than leaving you to chase it."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - timing, helper already here",
        "question": "How long does a direct hire take if the helper is already in Singapore?",
        "answer": (
            "Usually about 2 to 3 weeks. A helper already here on a valid work permit "
            "transfers across without going through her embassy and without travelling, "
            "which is what makes it the shorter of the two routes. It is an estimate - "
            "it moves with how quickly the documents come back and with MOM's own "
            "processing."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - timing, helper overseas",
        "question": "How long does a direct hire take if the helper is overseas?",
        "answer": (
            "Usually about 4 to 6 weeks. A helper coming from overseas goes through her "
            "own country's embassy once the approval is issued and then has to travel, "
            "which is what makes it longer than bringing across someone already here. It "
            "is an estimate - it moves with embassy appointment availability, with how "
            "quickly documents come back, and with MOM's own processing."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - after approval, helper overseas",
        "question": "What happens after approval if the helper is overseas?",
        "answer": (
            "Her papers go through her own country's embassy, and what that involves "
            "depends on her nationality - the Philippines, Indonesia and Myanmar each "
            "run it differently. Once that is done we coordinate her travel and her "
            "arrival and her work permit is issued. She then registers for the "
            "Settling-In Programme within seven days of arriving, and we arrange her "
            "transport to the training centre."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - after approval, helper already here",
        "question": "What happens after approval if the helper is already in Singapore?",
        "answer": (
            "There is no embassy step and no travel to arrange. Her work permit is "
            "issued directly, and she registers for the Settling-In Programme within the "
            "required seven days if she has not already completed it. We then go through "
            "the handover with you, which is why this route is noticeably shorter than "
            "bringing someone in from overseas."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - bond and insurance",
        "question": "What insurance and security bond are needed for a direct hire?",
        "answer": (
            "MOM requires a security bond of $5,000 for a helper who is not Malaysian, "
            "and we arrange it as an insured bond so you are not putting the money up "
            "yourself. She also needs medical insurance and personal accident insurance "
            "meeting MOM's minimum coverage, which we arrange to MOM's specifications "
            "alongside her medical examination. A consultant will confirm the current "
            "minimums and what the cover costs."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - the Settling-In Programme",
        "question": "What is the Settling-In Programme?",
        "answer": (
            "It is a short course run for helpers arriving to work in Singapore, "
            "covering their rights, their safety and living here. She has to be "
            "registered for it within seven days of arriving. We handle the "
            "registration and arrange her transport to the training centre, so it is "
            "not something you have to organise."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - handover and close",
        "question": "What happens at the end of a direct hire?",
        "answer": (
            "We go through the employment contract to make sure it covers everything MOM "
            "requires - salary, rest days, duties and termination terms - so nothing is "
            "left vague. Then we complete the handover pack and the orientation "
            "checklist with you and your helper, collect her work permit card and pass "
            "it to you, and close the case."
        ),
    },
]


# Rows that already exist and whose answer has been SUPERSEDED. ROWS above is
# purely additive and skips on question + service_type, which is right for new
# material and wrong for a row that has become untrue - leaving both in place
# puts a contradiction in front of the model and it will quote either one.
#
# Every entry states why. Nothing goes here to reword a row; only to correct
# one whose facts have changed or arrived.
UPDATES: list[dict[str, Any]] = [
    {
        "service_type": "direct_hiring",
        "question": "How long does a direct hire take?",
        # Written on 2026-09-07, when the agency had given no direct-hire
        # timeline and inventing one would have been binned by
        # ungrounded_figures. It said outright "there is no fixed timeline".
        # The agency supplied the timeline on 2026-09-08 - 2 to 3 weeks for a
        # helper already here, 4 to 6 for one overseas - so the old row now
        # contradicts the two new ones and denies an answer we hold.
        "reason": "the agency supplied the direct-hire timeline on 2026-09-08",
        "answer": (
            "It depends on where she is. If she is already in Singapore on a valid work "
            "permit, usually about 2 to 3 weeks - she transfers across without an "
            "embassy step and without travelling. If she is coming from overseas, "
            "usually about 4 to 6 weeks, because her papers go through her own country's "
            "embassy once approval is issued and she then has to travel. Both are "
            "estimates, and they move with document turnaround, embassy appointments and "
            "MOM's own processing."
        ),
    },
]

async def _existing_shape() -> tuple[set[str], str | None]:
    """Confirm the live column set and namespace instead of assuming them."""
    sample = await db.select_one(KB_TABLE, "*", is_active=True)
    if not sample:
        raise SystemExit(
            f"{KB_TABLE} returned no active row — refusing to write into a table "
            "whose shape I cannot confirm."
        )
    return set(sample.keys()), sample.get("namespace")


async def main(dry_run: bool) -> None:
    columns, namespace = await _existing_shape()
    logger.info("%s has %d columns; namespace=%r", KB_TABLE, len(columns), namespace)

    required = {"question", "answer", "service_type", "nationality", "embedding"}
    missing = required - columns
    if missing:
        raise SystemExit(f"{KB_TABLE} is missing expected column(s): {sorted(missing)}")

    written = skipped = 0
    for row in ROWS:
        already = await db.select_one(
            KB_TABLE, "id", question=row["question"], service_type=row["service_type"]
        )
        if already:
            logger.info("SKIP  (already present) %s", row["question"])
            skipped += 1
            continue

        payload: dict[str, Any] = {
            "namespace": namespace,
            "service_type": row["service_type"],
            "contact_type": "all",
            "nationality": row["nationality"],
            "chunk_type": "qa_pair",
            "question": row["question"],
            "answer": row["answer"],
            "content": f"{row['question']}\n{row['answer']}",
            "source_document": SOURCE_DOCUMENT,
            "section_heading": row["section_heading"],
            "is_active": True,
        }
        # Only send columns the table actually has.
        payload = {k: v for k, v in payload.items() if k in columns}

        if dry_run:
            logger.info(
                "WOULD WRITE  service=%s nat=%s  %s",
                row["service_type"],
                row["nationality"],
                row["question"],
            )
            written += 1
            continue

        # Embedded on the question+answer together: clients ask the question,
        # but the figures that make the row worth retrieving are in the answer.
        payload["embedding"] = await embed_query(payload["content"])
        await db.insert(KB_TABLE, payload)
        logger.info("WROTE  service=%s nat=%s", row["service_type"], row["nationality"])
        written += 1

    # Corrections come after the inserts, so a row added this run can also be
    # corrected this run if it ever needs to be.
    updated = 0
    for row in UPDATES:
        existing = await db.select_one(
            KB_TABLE,
            "id,answer",
            question=row["question"],
            service_type=row["service_type"],
        )
        if not existing:
            logger.warning(
                "UPDATE target missing, nothing to correct: %s", row["question"]
            )
            continue
        if (existing.get("answer") or "").strip() == row["answer"].strip():
            logger.info("SKIP  (already correct) %s", row["question"])
            continue
        if dry_run:
            logger.info("WOULD UPDATE  %s  (%s)", row["question"], row["reason"])
            updated += 1
            continue
        content = f"{row['question']}\n{row['answer']}"
        await db.update(
            KB_TABLE,
            {
                "answer": row["answer"],
                "content": content,
                # Re-embedded, or the row would still be retrieved on the old
                # wording and answer with the new text - or worse, not be
                # retrieved at all for the question it now answers.
                "embedding": await embed_query(content),
            },
            id=existing["id"],
        )
        logger.info("UPDATED  %s  (%s)", row["question"], row["reason"])
        updated += 1

    verb = "would write" if dry_run else "wrote"
    logger.info(
        "Done — %s %d row(s), skipped %d already present, %s %d row(s).",
        verb, written, skipped,
        "would correct" if dry_run else "corrected", updated,
    )
    if not dry_run and written:
        logger.info(
            "Now run:  python scripts/check_retrieval.py   "
            "and confirm a passport/transfer question retrieves these."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be written without touching the database",
    )
    asyncio.run(main(parser.parse_args().dry_run))
