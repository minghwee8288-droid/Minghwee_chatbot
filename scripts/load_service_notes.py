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
            "arrival and her work permit is issued. We then register her for the "
            "Settling-In Programme within the window MOM allows and arrange her "
            "transport to the training centre, so it is not something you have to "
            "organise."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - after approval, helper already here",
        "question": "What happens after approval if the helper is already in Singapore?",
        "answer": (
            "There is no embassy step and no travel to arrange. Her work permit is "
            "issued directly, and we register her for the Settling-In Programme "
            "within the window MOM allows if she has not already completed it. We "
            "then go through the handover with you, which is why this route is "
            "noticeably shorter than bringing someone in from overseas."
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
    # --- 2026-09-08: work permit renewal, documents and the full process ----
    #
    # Filed under `renewal`, which is the work-permit-renewal service key.
    #
    # The KB already held general FAQ material on renewals - validity, what
    # happens if it lapses, the 6-monthly medical - but not the agency's own
    # checklist, and in particular not the MOM RENEWAL NOTIFICATION, which the
    # source calls essential to apply at all. An employer who does not know to
    # look for it cannot start.
    #
    # Rewritten from the client's side as before. Dropped as internal: creating
    # and attaching the user account, and the instruction to buy insurance
    # before 5pm so it clears overnight. The overnight wait itself is kept,
    # because it is why the submission happens the following day and that is
    # the client's business; the 5pm cutoff is ours.
    #
    # Every figure here is small (8 weeks, 1 week, 2 weeks, 6-monthly), so none
    # trips ungrounded_figures, and all of them come from the agency's own
    # flow. `renewal` is a small-ticket service and is NOT in
    # COST_WITHHELD_SERVICES, so it may quote costs freely - but there is still
    # no agency fee for work permit renewal anywhere in the KB, so none of
    # these rows names one. Load the fee and it will quote it with no code
    # change.
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - documents from the employer",
        "question": "What documents do I need to renew my helper's work permit?",
        "answer": (
            "Five things from you: a copy of your NRIC, the Renewal Notification MOM "
            "sends you before the permit expires, your helper's passport, her new "
            "salary, and the number of rest days she will have. You also authorise us "
            "through the MOM website so we can submit on your behalf. The Renewal "
            "Declaration form is ours to prepare - you only sign it."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - the Renewal Notification",
        "question": "What is the Renewal Notification for a work permit?",
        "answer": (
            "It is the notice MOM sends you roughly 8 weeks before your helper's work "
            "permit expires, and the renewal cannot be applied for without it. When it "
            "arrives, send us a copy and we can start. If you think it is due and you "
            "have not seen it, tell us and we will look into it rather than leaving it "
            "to the last minute."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - documents we prepare",
        "question": "What documents does Ming Hwee prepare for a work permit renewal?",
        "answer": (
            "The Renewal Declaration form, which you sign electronically. We complete "
            "it, add the new insurance policy number and its expiry date once the "
            "insurance is in place, and upload it to MOM with the submission. "
            "Everything else on the renewal is paperwork you already hold rather than "
            "anything you have to draw up."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - the steps",
        "question": "What are the steps to renew my helper's work permit?",
        "answer": (
            "There are six. First, MOM sends you the Renewal Notification before the "
            "permit expires and you pass us a copy. Second, we collect what we need "
            "from you - your NRIC copy, her passport, her new salary and her number of "
            "rest days. Third, we send you an authorisation request to approve on the "
            "MOM site so we can act for you. Fourth, you sign the Renewal Declaration. "
            "Fifth, we arrange her insurance, add the new policy details to the "
            "declaration and submit the renewal to MOM. Sixth, the renewal "
            "confirmation and a temporary work permit come through and we send them "
            "to you. Tell us which step you would like more detail on."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - when to start",
        "question": "When should I start renewing my helper's work permit?",
        "answer": (
            "As soon as the Renewal Notification arrives, which MOM sends roughly 8 "
            "weeks before the permit expires. Starting then leaves room for the "
            "authorisation, the insurance and the submission without anything being "
            "rushed. Letting a permit lapse is not a small thing - it leaves your "
            "helper working illegally - so it is worth acting on the notice rather "
            "than filing it."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - the authorisation",
        "question": "What is the e-authorisation for a work permit renewal?",
        "answer": (
            "It is how you give us permission to submit the renewal to MOM on your "
            "behalf. We send you a request, and you approve it on the MOM website. The "
            "link stays valid for 1 week, so it is worth doing when it arrives - if it "
            "lapses we simply send a new one. Once you have authorised it, there is a "
            "2-week window to get the renewal completed, which is comfortably enough "
            "for the rest of it."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - after you authorise",
        "question": "What happens after I authorise the work permit renewal?",
        "answer": (
            "You sign the Renewal Declaration, and we arrange your helper's insurance. "
            "That processes overnight, so the renewal transaction goes through the "
            "following day. We add the new policy number and expiry date to the signed "
            "declaration, upload it to the MOM website and submit. The temporary work "
            "permit and the renewal confirmation are generated on submission, and we "
            "send the confirmation on to you."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - confirmation",
        "question": "How do I know my helper's work permit renewal has gone through?",
        "answer": (
            "A renewal confirmation and a temporary work permit are generated the "
            "moment the submission goes in, and we forward the confirmation to you so "
            "you have it in writing. The temporary permit covers her while the new card "
            "is produced. You do not need to chase us for it."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - what the employer does",
        "question": "What do I need to do myself for my helper's work permit renewal?",
        "answer": (
            "Four things, and none of them takes long. Send us the Renewal "
            "Notification when MOM sends it to you, along with a copy of your NRIC and "
            "your helper's passport. Tell us her new salary and how many rest days she "
            "will have. Approve the authorisation request on the MOM site within the "
            "week it stays valid. Then sign the Renewal Declaration we prepare. We "
            "handle the insurance, the submission and the confirmation."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - the medical examination",
        "question": "Does my helper need a medical examination for the work permit renewal?",
        "answer": (
            "Your helper has a medical examination every 6 months throughout her "
            "employment, and we coordinate it for you as it falls due rather than "
            "leaving you to track the dates. It runs alongside the renewal rather than "
            "being part of the submission itself."
        ),
    },
    # --- 2026-09-08: passport renewal, the per-nationality checklist --------
    #
    # The agency's own quick-reference and document checklist. Additions only
    # here; the rows it CONTRADICTS are corrected in UPDATES below.
    #
    # The fee finally exists. "No agency fee for passport renewal anywhere in
    # the KB" has been the standing gap since 2026-09-04, and it is why a cost
    # question on this service could only ever be deferred. $450 for a Filipino
    # and for an Indonesian helper. NO Myanmar fee was given, so none is
    # stated - passport_renewal is a small-ticket service and is not in
    # COST_WITHHELD_SERVICES, so what is here goes out, and inventing the
    # missing third would go out too.
    #
    # Kept OUT as internal: "always confirm the appointment date with the
    # runner first" and "check available appointment dates with the runner
    # before advising the client". Those instruct our staff. The client-facing
    # fact underneath - that we confirm the appointment before committing to a
    # date - is in the row below, which is the part that affects them.
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - cost",
        "question": "How much does it cost to renew my helper's passport?",
        "answer": (
            "It is $450 for a Filipino helper and $450 for an Indonesian helper. That "
            "covers us handling the embassy paperwork, the forms and the runner who "
            "takes her through the appointment. If your helper is of another "
            "nationality, tell us and a consultant will confirm the cost for her "
            "embassy."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - original passport or a copy",
        "question": "Do you need my helper's original passport?",
        "answer": (
            "It depends on her nationality. For a Filipino helper, yes - the embassy "
            "requires her ORIGINAL passport, not a copy, so we will need to hold it "
            "for the renewal. For an Indonesian helper a copy of the passport is "
            "enough. Either way we also need a copy of your NRIC and a copy of her "
            "work permit."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - when to start",
        "question": "When should I start my helper's passport renewal?",
        "answer": (
            "For a Filipino helper, start about 2 months before you need the new "
            "passport - the embassy appointment and the processing together take that "
            "sort of time. For an Indonesian helper it is quicker, but the appointment "
            "still has to be available. We check the appointment dates before giving "
            "you a date to work to, rather than promising one and moving it."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - forms we prepare",
        "question": "What forms does Ming Hwee prepare for my helper's passport renewal?",
        "answer": (
            "For a Filipino helper we prepare the embassy set - the contract, the "
            "Undertaking Form, Annex A, the passport renewal form and the OFW "
            "Information Sheet - and return them to the embassy with original "
            "signatures on them, so those cannot be signed electronically or "
            "photocopied. For an Indonesian helper we provide the passport form for "
            "completing and signing. You do not have to source any of these yourself."
        ),
    },

    # --- 2026-09-08: home leave -----------------------------------------------
    #
    # The KB held three home-leave rows and none of them was operational: is it
    # compulsory, who pays for the flights, and one untitled chunk. No document
    # list, no process, no fee, no lead time - so every practical question about
    # a home leave got the holding line, exactly as passport renewal did before
    # 2026-09-07.
    #
    # The route is decided by nationality and it changes MORE here than it does
    # for a passport renewal: the Philippines needs the ORIGINAL passport, a
    # ticket itinerary and six embassy forms returned with original signatures,
    # takes about 4 weeks and costs $400; Indonesia needs copies and one form we
    # provide, takes about 2 weeks and costs $250. Quote the wrong one and the
    # client has the wrong price, the wrong deadline and the wrong paperwork.
    # info_collector._ROUTE_BY_NATIONALITY covers the turns where we do not yet
    # know which of the two applies.
    #
    # Kept OUT as internal: "confirm embassy appointment availability with the
    # runner" instructs our staff. The client-facing fact underneath - that we
    # check what is available before giving them a date - is in the rows below.
    #
    # No Myanmar fee, timeline or document list was given, so none is stated.
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - what it involves",
        "question": "What is home leave and what does Ming Hwee handle?",
        "answer": (
            "Home leave is your helper going back to her home country between "
            "contracts and then returning to work for you. We handle the embassy "
            "endorsement, the levy waiver for the period she is away, deferring her "
            "six-monthly medical if one falls due while she is out, and coordinating "
            "her flights and her return. The embassy endorsement itself is usually "
            "approved within about 10 working days, but the overall lead time is "
            "longer and depends on her nationality."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - documents",
        "question": "What documents are needed for my helper's home leave?",
        "answer": (
            "It depends on her nationality. For a Filipino helper we need a copy of "
            "your NRIC, a copy of her work permit, her ORIGINAL passport and her "
            "ticket itinerary, plus the embassy set we prepare - the contract, the "
            "Undertaking Form, Annex A, the Balik-Manggagawa Information Sheet, the "
            "OFW Information Sheet and the Home Leave OEC form - all returned with "
            "original signatures. For an Indonesian helper we need a copy of your "
            "NRIC, a copy of her work permit and a copy of her passport, plus the "
            "Home Leave form we provide for her to complete and sign. Tell us her "
            "nationality and we can be exact."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "PH",
        "section_heading": "Home leave - Philippines documents",
        "question": "What documents does a Filipino helper need for home leave?",
        "answer": (
            "From you we need a copy of your NRIC, a copy of her work permit, her "
            "ORIGINAL passport rather than a copy, and her ticket itinerary. We "
            "prepare the embassy set - the contract, the Undertaking Form, Annex A, "
            "the Balik-Manggagawa Information Sheet, the OFW Information Sheet and "
            "the Home Leave OEC form - and each of those has to come back to us with "
            "an original signature on it, not a scan or a photocopy. Allow about 4 "
            "weeks, because an embassy appointment is required."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "ID",
        "section_heading": "Home leave - Indonesia documents",
        "question": "What documents does an Indonesian helper need for home leave?",
        "answer": (
            "From you we need a copy of your NRIC, a copy of her work permit and a "
            "copy of her passport - the original is not required. We provide the Home "
            "Leave form for her to complete and sign, and we deal with the embassy "
            "ourselves. Allow about 2 weeks."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - process",
        "question": "What is the process for arranging my helper's home leave?",
        "answer": (
            "Six steps. First we take her passport and contract details and confirm "
            "her nationality and the dates she wants to travel. Second we check what "
            "embassy appointments are actually available and tell you the lead time - "
            "about 4 weeks for a Filipino helper, about 2 weeks for an Indonesian "
            "one. Third we ask you for the documents her embassy needs, which differ "
            "by nationality. Fourth we prepare the endorsement forms, get the "
            "signatures required and submit them. Fifth we process the levy waiver "
            "for the period she is away and defer her six-monthly medical if one "
            "falls due. Sixth we coordinate her flights and her return, and keep you "
            "updated from submission through to the day she is back."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - timing",
        "question": "How long does it take to arrange home leave?",
        "answer": (
            "It depends on her nationality. For a Filipino helper allow about 4 "
            "weeks, because an embassy appointment has to be booked. For an "
            "Indonesian helper allow about 2 weeks. The endorsement itself is usually "
            "approved within about 10 working days once it is in. We check what "
            "appointments are available before giving you a date to work to, so the "
            "earlier we have the documents the more choice there is."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - cost",
        "question": "How much does home leave cost?",
        "answer": (
            "It is $400 for a Filipino helper and $250 for an Indonesian helper. That "
            "covers the embassy endorsement and the paperwork we handle for you. "
            "Flights are separate, and who pays for them follows what your employment "
            "contract says. If a Filipino helper's application has to be rushed there "
            "is an additional $40, and the embassy will want proof of the urgency or "
            "a letter explaining it. If your helper is of another nationality, tell "
            "us and a consultant will confirm the cost for her embassy."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "PH",
        "section_heading": "Home leave - urgent requests",
        "question": "Can my helper's home leave be arranged urgently?",
        "answer": (
            "For a Filipino helper, yes - there is an additional $40 on top of the "
            "$400 for an urgent request, and the embassy will want proof of the "
            "urgency or a letter of explanation setting out why it cannot wait, so "
            "have that ready. Without an urgent request, allow the usual 4 weeks "
            "because the appointment still has to be booked."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - levy",
        "question": "Do I still pay the levy while my helper is on home leave?",
        "answer": (
            "The levy is waived for the period she is away on home leave. That waiver "
            "is automatic and we process it as part of the arrangement, so there is "
            "nothing separate for you to file."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - six-monthly medical",
        "question": "What happens to the six-monthly medical while my helper is on home leave?",
        "answer": (
            "If one falls due while she is away it is deferred, and we arrange that as "
            "part of the same request rather than leaving you to sort it out. She "
            "takes it after she is back."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - what the employer does",
        "question": "What do I need to do as the employer for my helper's home leave?",
        "answer": (
            "Tell us her nationality and the dates she wants to travel, then give us "
            "the documents her embassy needs. A copy of your NRIC and a copy of her "
            "work permit either way; a Filipino helper also needs her original "
            "passport and her ticket itinerary, an Indonesian helper needs a copy of "
            "her passport. After that you sign the forms we prepare and return them "
            "to us, and for a Filipino helper those need original signatures. We take "
            "care of the embassy, the levy waiver, the medical deferral and the "
            "flight coordination."
        ),
    },
    {
        "service_type": "home_leave",
        "nationality": "all",
        "section_heading": "Home leave - flights",
        "question": "Does Ming Hwee arrange the flights for home leave?",
        "answer": (
            "We coordinate the flight booking and her return travel, and we keep you "
            "updated from the day the application goes in through to the day she is "
            "back. Who pays for the ticket is a separate matter and follows what your "
            "employment contract says."
        ),
    },

    # --- 2026-09-08: replacement ----------------------------------------------
    #
    # Fourteen rows already carried service_type='replacement' and not one of
    # them described how a replacement is actually done. Two are FAQ answers
    # (the guarantee, and what to do about performance) and TWELVE are raw
    # clauses lifted from the Client Service Agreement - refund percentages,
    # entitlement tables, "subject to the conditions in Clause 4".
    #
    # So clause 3.1 - "the Employer is entitled to two (2) replacement(s) of MDW
    # within a period of six (6) months" - was the TOP match for seven different
    # questions, measured 2026-09-08 under service=replacement: "what is the
    # process" 0.417, "what documents are needed" 0.443, "what forms do i have
    # to sign" 0.438, "how does a replacement work" 0.483, "what happens when
    # she arrives" 0.489, "how long does a replacement take" 0.484. Five of
    # those clear the 0.40 floor, so _answerable() read True and the widening
    # retry never fired: a client asking what paperwork to gather would have
    # been read a refund-entitlement clause, confidently.
    #
    # The incoming candidate's half MIRRORS NEW HIRING, in the agency's own
    # words, and the shared MOM steps were moved to 'general' on 2026-09-08 for
    # direct hire - so they are already reachable from here (measured: "what is
    # an IPA" 0.530, "what is the Settling-In Programme" 0.554). Nothing is
    # duplicated for that reason. Only what is genuinely replacement-specific
    # is below: the two forms that replace the new-hire fee schedule, the
    # document checklist, and the nine steps.
    #
    # Kept OUT as internal: creating and attaching the employer account, the
    # dashboard that shows the matched profiles, the partnering agent by that
    # name, notifying the transport company, and case closure. Those describe
    # our own workflow to the person it is being run on.
    #
    # NO replacement fee is stated. The source names a "Replacement Services &
    # Fees form" but gives no amount, and the existing FAQ row already says a
    # replacement inside the guarantee period carries no additional agency
    # service fee. Inventing a figure to sit beside that is how the KB ends up
    # contradicting itself.
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - what it is",
        "question": "What is a replacement and how does it work?",
        "answer": (
            "A replacement is swapping the helper you have now for a different one, "
            "usually because she is not the right fit for your household. For the "
            "incoming helper it runs like a new hire - we shortlist candidates against "
            "your revised requirements, you interview and confirm one, and we handle "
            "the Work Permit application, the insurance and security bond, her medical "
            "and her arrival. What differs is the paperwork at your end: you sign a "
            "Replacement form and a Replacement Services and Fees form rather than the "
            "full new-hire fee schedule."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - documents from the employer",
        "question": "What documents are needed for a replacement?",
        "answer": (
            "From you we need a copy of your NRIC and proof of income - either your "
            "Income Tax Assessment or a Declaration of Monthly Income. If you are a "
            "foreign employer that is your Employment Pass or S Pass together with "
            "your passport, or a company letter together with your tenancy agreement. "
            "If this would be an additional helper we also need identification copies "
            "for the children or elderly in your care. For the incoming helper we need "
            "her passport copy, her medical report and her school certificate, and "
            "those come to us through her agency rather than from you."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - forms we prepare",
        "question": "What forms does Ming Hwee prepare for a replacement?",
        "answer": (
            "Seven, and we prepare all of them for you to sign electronically: the "
            "Replacement form, the Replacement Services and Fees form, the "
            "Authorisation Form, the Employer Particulars form, the last page of the "
            "helper's biodata, the Job Offer Form, and her Employment History if she "
            "has worked in Singapore before. You do not have to source any of these "
            "yourself."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - process",
        "question": "What is the process for replacing my helper?",
        "answer": (
            "There are nine steps. First we go through your household again and your "
            "revised requirements, since whatever is not working usually changes them. "
            "Second we shortlist the closest matching candidates and arrange a video "
            "interview so you can meet them. Third, once you confirm the one you want, "
            "you sign the Replacement form. Fourth you authorise us through Singpass "
            "to deal with MOM on your behalf and give us your personal details. Fifth "
            "we prepare the rest of the set for you to sign electronically. Sixth we "
            "send the Job Offer Form to her agency for her to sign, collect her "
            "passport copy, medical report and school certificate, and confirm she is "
            "medically fit. Seventh we submit the application to MOM, and once the IPA "
            "is issued both you and she sign it and we upload it, which is what allows "
            "the Work Permit to be issued. Eighth we arrange her insurance and the "
            "security bond and check when you are free to collect her. Ninth she "
            "arrives, has her medical and her Settling-In Programme, and we hand over "
            "to you."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - what the employer does",
        "question": "What do I need to do myself for a replacement?",
        "answer": (
            "Five things. Tell us what needs to change about the requirements, so we "
            "are not matching you against the same brief. Sit in on the video "
            "interviews and confirm the helper you want. Authorise us through Singpass "
            "so we can transact with MOM for you. Sign the set we prepare, including "
            "the Replacement form and the IPA when it is issued. And be available to "
            "collect her when she arrives. We do the rest."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - after you confirm the candidate",
        "question": "What happens after I choose the replacement helper?",
        "answer": (
            "You sign the Replacement form, then authorise us through Singpass to "
            "transact with MOM on your behalf and give us your personal details. We "
            "prepare the rest of the documents for you to sign electronically, send "
            "the Job Offer Form to her agency for her to sign, and collect her "
            "passport copy, medical report and school certificate to confirm she is "
            "medically fit. Then the application goes to MOM."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - how it differs from a new hire",
        "question": "How is a replacement different from hiring a new helper?",
        "answer": (
            "For the incoming helper it is the same job - matching, the interview, the "
            "MOM application, the insurance and security bond, the medical, the "
            "Settling-In Programme and the handover all run exactly as they do for a "
            "new hire. The difference is what you sign: a Replacement form and a "
            "Replacement Services and Fees form instead of the full new-hire fee "
            "schedule. We also start from your revised requirements rather than a "
            "blank brief, since we already know your household."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - interviewing",
        "question": "Do I get to interview the replacement helper?",
        "answer": (
            "Yes. We shortlist the candidates who most closely match your revised "
            "requirements and arrange a video interview so you can meet them before "
            "deciding. If none of them is right we go back to the requirements and "
            "look again rather than pressing you to take one."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - the incoming helper's documents",
        "question": "What do you need from the incoming replacement helper?",
        "answer": (
            "Her passport copy, her medical report and her school certificate, and a "
            "signed Job Offer Form. Those come to us through her agency, so there is "
            "nothing for you to chase. We confirm she is medically fit before the "
            "application goes to MOM."
        ),
    },
    # Shared with every service that brings a helper in on a new Work Permit,
    # so filed as 'general' rather than copied - the same decision as the eight
    # rows moved there on 2026-09-08, and for the same reason (§9.8: duplicated
    # constants diverge). Measured before this row existed: "do i need to buy
    # insurance" scored 0.000 under service=replacement - nothing in the whole
    # knowledge base matched it, because the only row that answers it is
    # phrased "for a direct hire" and the filter excluded it.
    #
    # No insurance MINIMUM is stated - see CLAUDE.md §9.12, the KB disagrees
    # with itself three ways and it is Ming Hwee's call, not this script's. The
    # $5,000 bond is documented as quotable and is phrased the way the existing
    # direct-hire row phrases it, deliberately clear of the words
    # quotes_hiring_package_cost fires on.
    {
        "service_type": "general",
        "nationality": "all",
        "section_heading": "Insurance and security bond",
        "question": "Do I need to buy insurance and a security bond for my helper?",
        "answer": (
            "Both are required and we arrange both for you. MOM requires a security "
            "bond of $5,000 for a helper who is not Malaysian, and we put it in place "
            "as an insured bond so you are not laying the money out yourself. She also "
            "needs medical insurance and personal accident insurance at the coverage "
            "MOM sets, and we buy those before the Work Permit is issued. A consultant "
            "will confirm the coverage figures for you."
        ),
    },

    # --- 2026-09-08: the consolidated cost + timeline table -------------------
    #
    # The agency's summary of what every service costs and how long it takes.
    # Two kinds of entry, and they are handled differently.
    #
    # WHERE A FEE IS STATED it goes in: work permit renewal $695, and this
    # closes the last standing content gap - "no agency fee for work permit
    # renewal anywhere in the KB" has been open since 2026-09-04. Passport
    # renewal ($450) and home leave ($400 / $250) were already loaded and match.
    #
    # WHERE A FEE IS NOT STATED nothing is invented. Their instruction: "the
    # service which do not have the timeline and cost that means we dont have to
    # open that live agent will handle that". New hiring, direct hiring,
    # replacement and transfer therefore defer to a consultant, and the three
    # that were not already withheld mechanically joined COST_WITHHELD_SERVICES
    # in the same change.
    #
    # Three EXISTING rows contradicted the table and were corrected rather than
    # stacked - see UPDATES at the foot of this file.
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - cost",
        "question": "How much does it cost to renew my helper's work permit?",
        "answer": (
            "Work permit renewal is $695. That covers us handling the whole thing for "
            "you - the authorisation, the declaration, the insurance arrangement and "
            "the submission to MOM. Your helper's insurance premium and her medical "
            "examination are separate, since those are paid to the insurer and the "
            "clinic rather than to us."
        ),
    },
    {
        "service_type": "renewal",
        "nationality": "all",
        "section_heading": "Work permit renewal - how long it takes",
        "question": "How long does a work permit renewal take?",
        "answer": (
            "About a week from the point we have what we need from you, and often "
            "faster - the processing itself usually runs around 3 days. The part "
            "worth planning around is not the processing but the start: MOM sends you "
            "the Renewal Notification roughly 8 weeks before the permit expires, and "
            "the sooner we have that and your authorisation, the more room there is."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - how long it takes",
        "question": "How long does a replacement take?",
        "answer": (
            "Around 4 to 6 weeks if the replacement helper is coming from overseas, "
            "which covers the matching and interviews, the MOM application, her "
            "insurance and bond, and her travel and arrival. A helper already in "
            "Singapore is quicker, because there is no embassy stage and no flight. "
            "The biggest variable at the front is how quickly you settle on a "
            "candidate."
        ),
    },
    {
        "service_type": "replacement",
        "nationality": "all",
        "section_heading": "Replacement - cost",
        "question": "How much does a replacement cost?",
        "answer": (
            "That depends on your original agreement and the circumstances, so a "
            "consultant will confirm it rather than have me give you half a picture. "
            "What I can tell you is that a replacement within your guarantee period "
            "carries no additional agency service fee, and that you sign a Replacement "
            "Services and Fees form rather than the full new-hire schedule. Government "
            "and third-party costs such as insurance and the medical are separate "
            "either way."
        ),
    },
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer - how long it takes",
        "question": "How long does it take from interview to my helper starting?",
        "answer": (
            "Around 1 to 2 weeks from the interview to her starting work. Inside that, "
            "the MOM approval itself usually takes 1 to 3 working days, and once it is "
            "approved we purchase the required insurance and she can start the "
            "following day. It stretches if MOM asks for additional documents."
        ),
    },
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer - cost",
        "question": "How much does a transfer cost?",
        "answer": (
            "There is a transfer fee, and a consultant will confirm the amount for "
            "your situation rather than have me quote you something that turns out not "
            "to apply. It is a good deal less involved than a full overseas "
            "recruitment, since there is no embassy stage and no flight. Government "
            "and third-party costs such as the insurance are separate."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - cost",
        "question": "How much does a direct hire cost compared to hiring through the agency?",
        "answer": (
            "A direct hire costs less than a full recruitment, because you have "
            "already found the helper yourself and there is no sourcing, matching or "
            "interviewing for us to do. What you are paying for is the processing - "
            "the MOM application, the documents, the insurance and bond, and getting "
            "her here and settled. A consultant will confirm the exact figure for your "
            "case."
        ),
    },
]


# Rows that already exist and need CHANGING rather than adding. ROWS above is
# purely additive and skips on question + service_type, which is right for new
# material and wrong for a row that has become untrue, or that is filed where
# only one of the two services that need it can see it.
#
# `where` locates the row; `set` is what changes. Every entry states why.
# Nothing goes here to reword a row.
#
# Idempotent in both directions: if `set` moves the row's service_type, a
# re-run looks for it under the NEW value too and skips when it is already
# there, so this can be run as often as ROWS can.
UPDATES: list[dict[str, Any]] = [
    {
        "where": {"question": "How long does a direct hire take?",
                  "service_type": "direct_hiring"},
        # Written on 2026-09-07, when the agency had given no direct-hire
        # timeline and inventing one would have been binned by
        # ungrounded_figures. It said outright "there is no fixed timeline".
        # The agency supplied it on 2026-09-08 - 2 to 3 weeks for a helper
        # already here, 4 to 6 for one overseas - so the old row contradicted
        # the two new ones and denied an answer we hold.
        "reason": "the agency supplied the direct-hire timeline on 2026-09-08",
        "set": {
            "answer": (
                "It depends on where she is. If she is already in Singapore on a valid "
                "work permit, usually about 2 to 3 weeks - she transfers across without "
                "an embassy step and without travelling. If she is coming from "
                "overseas, usually about 4 to 6 weeks, because her papers go through "
                "her own country's embassy once approval is issued and she then has to "
                "travel. Both are estimates, and they move with document turnaround, "
                "embassy appointments and MOM's own processing."
            ),
        },
    },
]

# The agency's own statement of the difference, 2026-09-08: "No candidate
# sourcing, matching or interviews. Everything else - MOM submission, bond,
# insurance, medical, embassy (overseas only), SIP, handover - mirrors New
# Hiring."
#
# Those shared steps were all filed under `new_hiring`, and the match function
# filters `service_type in (filter, 'general')` - so from inside a direct-hire
# conversation they were INVISIBLE. Measured before this change, with the
# filter set to direct_hiring: "what is an IPA" 0.474, "do I need to attend a
# course" 0.499, "what happens at the embassy after the IPA" 0.551, "what
# happens when she arrives" 0.702. Every one of them clears the 0.40 floor, so
# `_answerable()` read True and the widening retry in rag_retriever - which
# only fires BELOW the floor - never ran. The bot would have answered
# confidently from whichever direct_hiring row happened to score best: the top
# match for "what happens when she arrives" was "How long does a direct hire
# take?".
#
# Filed as 'general' rather than duplicated under direct_hiring. Duplication is
# how two copies drift apart (see CLAUDE.md section 9.8), and these are MOM
# steps that will change for both services at once when they change at all.
# 'general' is an established bucket, 95 rows before this.
#
# Deliberately NOT moved, because they are exactly what direct hire does not
# have: "How do you match me with a helper?", "Can I interview the helper
# before I decide?", "What are the stages of hiring a helper from start to
# finish?", "What is the process for hiring a new helper?", and "What do I need
# to do myself when hiring a helper?" - the last because its wording turns on
# choosing from a shortlist we sent.
_SHARED_WITH_DIRECT_HIRE = [
    "What is an IPA?",
    "Do I need to attend a course before hiring a helper?",
    "What happens at the embassy stage after the IPA is issued?",
    "What happens after the IPA for a Filipino helper?",
    "What happens after the IPA for an Indonesian helper?",
    "What happens after the IPA for a Myanmar helper?",
    "What happens before my helper flies to Singapore?",
    "What happens when my helper arrives in Singapore?",
]

UPDATES += [
    {
        "where": {"question": q, "service_type": "new_hiring"},
        "reason": "direct hire mirrors new hiring after sourcing (agency, 2026-09-08)",
        "set": {"service_type": "general"},
    }
    for q in _SHARED_WITH_DIRECT_HIRE
]



# The Settling-In Programme row already existed under new_hiring, better
# written than the one added here on 2026-09-08 and without a deadline we
# cannot verify. The duplicate was deleted; this shares the survivor.
UPDATES.append(
    {
        "where": {"question": "What is the Settling-In Programme?",
                  "service_type": "new_hiring"},
        "reason": "SIP is one of the steps direct hire shares with new hiring",
        "set": {"service_type": "general"},
    }
)

# The agency's flow states a SEVEN-DAY Settling-In Programme window. MOM's own
# requirement for a first-time helper is tighter, and a missed SIP registration
# is a penalty on the employer - so this is a regulatory deadline stated as
# fact to the person who would be penalised, on a figure nobody here has
# confirmed. Removed rather than corrected: we register her either way, so
# "within the window MOM allows" is true whatever the number turns out to be
# and costs the client nothing. Put the figure back once Ming Hwee confirms it.
UPDATES += [
    {
        "where": {"question": "What happens after approval if the helper is overseas?",
                  "service_type": "direct_hiring"},
        "reason": "the seven-day SIP window is unconfirmed and the penalty falls on the employer",
        "set": {"answer": (
            "Her papers go through her own country's embassy, and what that involves "
            "depends on her nationality - the Philippines, Indonesia and Myanmar each "
            "run it differently. Once that is done we coordinate her travel and her "
            "arrival and her work permit is issued. We then register her for the "
            "Settling-In Programme within the window MOM allows and arrange her "
            "transport to the training centre, so it is not something you have to "
            "organise."
        )},
    },
    {
        "where": {"question": "What happens after approval if the helper is already in Singapore?",
                  "service_type": "direct_hiring"},
        "reason": "the seven-day SIP window is unconfirmed and the penalty falls on the employer",
        "set": {"answer": (
            "There is no embassy step and no travel to arrange. Her work permit is "
            "issued directly, and we register her for the Settling-In Programme "
            "within the window MOM allows if she has not already completed it. We "
            "then go through the handover with you, which is why this route is "
            "noticeably shorter than bringing someone in from overseas."
        )},
    },
]


# --- 2026-09-08: the passport-renewal checklist supersedes the 2026-09-07 rows
#
# The agency sent a per-nationality document checklist that CONTRADICTS what
# was loaded on 2026-09-07 from their process-flow document. Two real
# differences, both with consequences at an embassy counter:
#
#   * The Philippines needs the helper's ORIGINAL passport. The old rows said
#     "a copy of her passport" for every nationality. Turning up with a copy
#     when the original is required wastes the appointment, and a Filipino
#     appointment is roughly 2 months out.
#
#   * The old rows made the Undertaking of Employer Form exclusive to helpers
#     WITHOUT an embassy contract - i.e. Myanmar. The new checklist lists an
#     Undertaking Form among the PHILIPPINES embassy documents, alongside
#     Annex A and the OFW Information Sheet, which the old rows never mentioned
#     at all.
#
# The newer document is the explicit per-nationality checklist, so it is taken
# as authoritative for PH and ID. Myanmar is NOT covered by it, so the Myanmar
# rows are left exactly as they were - but the rows that claimed those forms
# belong ONLY to the no-contract route are corrected, because that claim is now
# contradicted for the Philippines.
#
# Flagged for Ming Hwee rather than resolved here: whether the Myanmar
# three-form route still stands as the 2026-09-07 document described it.
UPDATES += [
    {
        "where": {"question": "What documents are needed to renew my helper's passport?",
                  "service_type": "passport_renewal"},
        "reason": "the per-nationality checklist supersedes the embassy-contract framing",
        "set": {"answer": (
            "It depends on her nationality. For a Filipino helper we need a copy of "
            "your NRIC, a copy of her work permit and her ORIGINAL passport, plus the "
            "embassy set we prepare - the contract, the Undertaking Form, Annex A, the "
            "passport renewal form and the OFW Information Sheet, all returned with "
            "original signatures. For an Indonesian helper we need a copy of your "
            "NRIC, a copy of her work permit and a copy of her passport, plus the "
            "passport form we provide for her to complete and sign. Tell us her "
            "nationality and we can be exact."
        )},
    },
    {
        "where": {"question": "What documents does a Filipino helper need for passport "
                              "renewal, and how does the embassy visit work?",
                  "service_type": "passport_renewal"},
        "reason": "PH needs the ORIGINAL passport and a wider embassy set than was loaded",
        "set": {"answer": (
            "From you we need a copy of your NRIC and a copy of her work permit, and "
            "we need her ORIGINAL passport rather than a copy. We prepare the embassy "
            "set - the contract, the Undertaking Form, Annex A, the passport renewal "
            "form and the OFW Information Sheet - and these must go back with original "
            "signatures on them. She is required to report to the Philippine embassy "
            "in person, so she makes her own way there and meets our runner at the "
            "embassy, who takes her through it. Afterwards the runner tells us it is "
            "done and we update you."
        )},
    },
    {
        "where": {"question": "What documents does an Indonesian helper need for passport "
                              "renewal, and how does the embassy visit work?",
                  "service_type": "passport_renewal"},
        "reason": "ID needs a passport copy and our own passport form, not the old generic set",
        "set": {"answer": (
            "From you we need a copy of your NRIC, a copy of her work permit and a "
            "copy of her passport - the original is not required. We provide the "
            "passport form for her to complete and sign. Our runner collects her from "
            "your home, takes her to the Indonesian embassy for the renewal and brings "
            "her back afterwards, so she is accompanied both ways. The runner then "
            "tells us it is complete and we update you."
        )},
    },
    {
        "where": {"question": "Does my helper have an embassy contract?",
                  "service_type": "passport_renewal"},
        "reason": "the Undertaking Form is not exclusive to the no-contract route",
        "set": {"answer": (
            "Filipino and Indonesian helpers hold an embassy contract; Myanmar helpers "
            "do not. It matters because the paperwork differs by nationality rather "
            "than being one list for everybody - a Filipino renewal goes in with the "
            "contract, the Undertaking Form, Annex A, the passport renewal form and "
            "the OFW Information Sheet, an Indonesian one with the passport form we "
            "provide, and a helper with no embassy contract needs the Undertaking of "
            "Employer Form, the Standard Employment Contract and the Information Sheet "
            "of Employer signed first. Tell us her nationality and we can be exact."
        )},
    },
    {
        "where": {"question": "What do I need to do as the employer for my helper's "
                              "passport renewal?",
                  "service_type": "passport_renewal"},
        "reason": "the employer's part is now nationality-specific, and PH hands over the original passport",
        "set": {"answer": (
            "Not much, and most of it is providing copies. We need a copy of your NRIC "
            "and a copy of her work permit. If she is Filipino we also need her "
            "ORIGINAL passport, and the embassy forms we prepare have to come back "
            "with original signatures; if she is Indonesian a copy of her passport is "
            "enough. The embassy trip itself is handled by a runner - he collects an "
            "Indonesian or Myanmar helper from your home and brings her back, and a "
            "Filipino helper reports to the embassy herself and meets him there. We "
            "let you know once the renewal is done."
        )},
    },
    {
        "where": {"question": "What is the process for renewing my helper's passport?",
                  "service_type": "passport_renewal"},
        "reason": "the agency supplied the full six-step process on 2026-09-08",
        "set": {"answer": (
            "Six steps. First we confirm her nationality and when her current passport "
            "expires, since the paperwork and the timing both follow from that. Second "
            "we check what embassy appointments are actually available before giving "
            "you a date. Third we ask you for the documents her embassy needs. Fourth "
            "we prepare the embassy forms and get the signatures they require. Fifth "
            "our runner submits everything to the embassy and we track the appointment "
            "and the processing. Sixth the renewed passport comes back and we return "
            "it to you. Tell us her nationality and we can be specific about the "
            "documents and the timing."
        )},
    },
]


# --- 2026-09-08: the consolidated cost + timeline table corrects three rows ---
#
# All three ANSWER the question the table answers, and all three answer it with
# a different number. Leaving them alongside puts a flat contradiction in front
# of a model that quotes either, which is the reason UPDATES exists.
UPDATES += [
    {
        "where": {"question": "How long does it take to hire a domestic helper in "
                              "Singapore?",
                  "service_type": "new_hiring"},
        "reason": "the agency's table gives 4-6 weeks; this row said 6-8 for an "
                  "overseas hire and 3-4 for a transfer already here, and the "
                  "transfer figure now disagrees with the transfer rows too (1-2 wks)",
        "set": {"answer": (
            "About 4 to 6 weeks from signing with us to your helper's first day, "
            "including the overseas processing. That covers shortlisting and "
            "interviews, the Work Permit application, her travel, and the medical and "
            "Settling-In Programme after she arrives. A helper already in Singapore is "
            "faster, because there is no embassy stage and no flight - tell us which "
            "you are considering and we can be more precise."
        )},
    },
    {
        "where": {"question": "How do I renew my helper's work permit?",
                  "service_type": "renewal"},
        "reason": "the agency's table gives about a week, ~3 days processing; this "
                  "row said 4 weeks, and it is the top answer to the renewal question",
        "set": {"answer": (
            "You need an updated employment contract, current insurance coverage, a "
            "recent medical examination, and the renewal submitted to MOM - and Ming "
            "Hwee handles all of it. What we need from you is the Renewal Notification "
            "MOM sends you, your authorisation through Singpass and a signature, which "
            "is well under an hour of your time. Once we have those it usually takes "
            "about a week, and the processing itself runs around 3 days."
        )},
    },
    {
        "where": {"question": "How long does a transfer take and what is the process?",
                  "service_type": "transfer"},
        "reason": "it gave only the MOM approval window (1-3 working days) as the "
                  "answer to how long a transfer takes; the agency's table gives 1-2 "
                  "weeks from interview to deployment, which is what a client is asking",
        "set": {"answer": (
            "Around 1 to 2 weeks from the interview to her starting work with the new "
            "employer. The MOM approval inside that usually takes 1 to 3 working days, "
            "unless MOM asks for additional documents to be uploaded, which adds to "
            "the wait. Once MOM approves it we purchase the required insurance, and "
            "after the insurance is transmitted she can start the following day."
        )},
    },
]


# --- 2026-09-09: the client meeting reworded the passport timings and fee ----
#
# Two things they asked for, both about what reaches the client rather than
# what is true:
#
#   * "approximately", not "roughly". Asked for by name.
#   * No embassy/appointment mechanics and no runner. Their exclusion list is
#     explicit - the bot must not explain that an appointment is booked, how it
#     is handled, or that we accompany her. It is internal processing, and it
#     was arriving before the client had even agreed to proceed.
#
# The timing rows are edited rather than the briefing alone, because the
# briefing quotes THESE, and an instruction not to mention a runner sitting
# beside a record that describes one is a fight the record usually wins.
#
# The nationality-specific PROCESS and embassy rows are deliberately LEFT: a
# client who asks outright "does someone go with her" still gets a straight
# answer. The exclusion is about what we volunteer, not about refusing to
# answer. Flagged for the agency in case they want those gone too.
UPDATES += [
    {
        "where": {"question": "How long does a passport renewal take for a helper?",
                  "service_type": "passport_renewal"},
        "reason": "2026-09-09 meeting: 'approximately' not 'roughly', and no embassy mechanics",
        "set": {"answer": (
            "It depends on her nationality. For a Filipino helper it is "
            "approximately 6 to 8 weeks. For an Indonesian helper it is "
            "approximately 3 working days. For a Myanmar helper it is "
            "approximately a day in person, though the wait for a slot can run "
            "to weeks or months. These are estimates and can vary."
        )},
    },
    {
        "where": {"question": "How long does passport renewal take for a Filipino helper?",
                  "service_type": "passport_renewal"},
        "reason": "same - and the printing/shipping detail is our processing, not theirs",
        "set": {"answer": (
            "Approximately 6 to 8 weeks. This is an estimate and can vary with "
            "appointment availability and document verification."
        )},
    },
    {
        "where": {"question": "How long does passport renewal take for an Indonesian helper?",
                  "service_type": "passport_renewal"},
        "reason": "same - the operating-hours and online-appointment detail is ours",
        "set": {"answer": (
            "Approximately 3 working days. This is an estimate and can vary with "
            "appointment availability and document verification."
        )},
    },
    {
        "where": {"question": "How long does passport renewal take for a Myanmar helper?",
                  "service_type": "passport_renewal"},
        "reason": "same wording; the wait for a slot is kept because it IS the timeline",
        "set": {"answer": (
            "Approximately a day in person once a slot is available, but the "
            "wait for one can run to weeks or months. This is an estimate and "
            "can vary with document verification."
        )},
    },
    {
        # "That covers us handling the embassy paperwork, the forms and the
        # runner who takes her through the appointment" - the runner is exactly
        # what the meeting asked to stop mentioning.
        "where": {"question": "How much does it cost to renew my helper's passport?",
                  "service_type": "passport_renewal"},
        "reason": "2026-09-09 meeting: the runner is an internal step and must not be quoted",
        "set": {"answer": (
            "It is approximately $450 for a Filipino helper and approximately "
            "$450 for an Indonesian helper. That covers us handling the "
            "paperwork and the forms from end to end. If your helper is of "
            "another nationality, tell us and a consultant will confirm the "
            "cost for her embassy."
        )},
    },
]

# Where each relocated row now lives, derived from UPDATES so the two can
# never disagree. Keyed by question, which is what the ROWS skip check has.
_RELOCATED: dict[str, str] = {
    u["where"]["question"]: u["set"]["service_type"]
    for u in UPDATES
    if "service_type" in u["set"]
}


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
        # A row this script later RELOCATES is no longer where ROWS says it is,
        # and the skip check keys on question + service_type. Live, 2026-09-08:
        # the second run of this script re-inserted all eight relocated rows as
        # duplicates, because they had moved to 'general' and so did not match
        # the 'new_hiring' the ROWS entry still declares. Look where the row
        # was MOVED to as well, or "idempotent" holds for exactly one run.
        if not already and row["question"] in _RELOCATED:
            already = await db.select_one(
                KB_TABLE, "id",
                question=row["question"],
                service_type=_RELOCATED[row["question"]],
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
        where, changes = row["where"], row["set"]
        existing = await db.select_one(KB_TABLE, "id,question,answer,service_type", **where)

        if not existing and "service_type" in changes:
            # Already moved on an earlier run. Look for it where it now lives
            # rather than reporting a missing target every time.
            moved = {**where, "service_type": changes["service_type"]}
            if await db.select_one(KB_TABLE, "id", **moved):
                logger.info("SKIP  (already applied) %s", where["question"])
                continue
        if not existing:
            logger.warning("UPDATE target missing: %s", where["question"])
            continue
        if all((existing.get(k) or "") == v for k, v in changes.items()):
            logger.info("SKIP  (already correct) %s", where["question"])
            continue
        if dry_run:
            logger.info(
                "WOULD UPDATE  %s -> %s  (%s)",
                where["question"], sorted(changes), row["reason"],
            )
            updated += 1
            continue

        payload = dict(changes)
        if "answer" in changes:
            # Re-embedded on the new text, or the row is still retrieved on its
            # old wording and then answers with the new - or is not retrieved
            # at all for the question it now answers.
            content = f"{existing['question']}\n{changes['answer']}"
            payload["content"] = content
            payload["embedding"] = await embed_query(content)
        await db.update(KB_TABLE, payload, id=existing["id"])
        logger.info("UPDATED  %s -> %s  (%s)", where["question"], sorted(changes), row["reason"])
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
