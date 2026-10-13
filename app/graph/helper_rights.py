"""A helper's rights, in detail - sent word for word, not written by the model.

The agency, 2026-10-12, on the ten-line summary: "i want detailed rights
message for helper in listed format properly", checked against the text they
gave us - Module 27, Helper Rights (Simple English). Every point below is in
that module; nothing is added, and its old WhatsApp number is replaced by the
current one (the knowledge base made the same change on 2026-09-11).

Fixed wording rather than a model reply, for three reasons:
  * It is about 3,300 characters. A knowledge-base entry is capped at 1,200
    by kb-admin (`kb_admin_save_draft`), so it cannot live in an entry the
    agency could edit, and a model asked to reproduce it drops points.
  * Its sub-points are bullets, which `looks_like_document` bans in any
    model reply.
  * It carries phone numbers and the 999 instruction, which must reach her
    exactly as written.

The short ten-line entry stays in the knowledge base: it is what retrieval
finds, and what a parked topic or an unknown sender is answered from.
"""

from __future__ import annotations

HELPER_RIGHTS_MESSAGE = """Here are your rights as a domestic helper in Singapore:

1. Your salary
• Your full salary every month, paid by the date in your contract (or within 7 days).
• Not OK: paying late more than once, paying less than the contract, or taking money for breakages, food or anything not in your contract.
• If it is not paid: tell your employer once, calmly. If it is still not paid, WhatsApp us and we will help. MOM can also help you get unpaid salary back.

2. Your rest day
• One full day off every week. You can agree to work that day for extra money - that is your choice.
• At least ONE real day off every month. This one cannot be changed to money, even if you ask. It is the law.
• Not OK: "no off-day for you", cancelling your off-day again and again, or paying you less than one day's salary instead.

3. Your passport
• Your passport belongs to you, and you should keep it.
• It can be kept in a household safe only if you agree and you know the code or have a key.
• Not OK: your employer keeps it, hides it, or says "I will give it back when the contract is over". This is against Singapore law.

4. Your food
• 3 meals a day, enough food, and food you can eat (no pork or no beef if you do not eat it).
• Not OK: being hungry every day, being given only leftovers, or not being allowed to eat the food you cook.

5. Your room and sleep
• A place to sleep with a mattress, pillow and blanket, a place for your things, and privacy when you change clothes.
• At least 7 hours of sleep at night.
• Not OK: sleeping on the floor with no mattress, in the kitchen or bathroom, or in the same room as a male family member.

6. Your phone
• You can have your own phone and talk to your family.
• "No phone during work" is fair. Taking your phone, "no phone ever", or reading your messages without permission is not OK.

7. Your placement loan
• You have the right to a copy of the loan paper you signed, in a language you understand, and to know your balance at any time.
• No more can be taken each month than you agreed, and your full salary must start again as soon as the loan is paid off.
• Owing money does not take away any of your rights.

8. Your safety
• No one may hit you, hurt you or threaten you.
• In an emergency call the police on 999. If you are very sick or very hurt, call an ambulance on 995. If you are locked in the house, call 999.

9. Protection from sexual harassment
• No one in the house may touch your body, ask you for sex, show you sexual pictures, or pressure you. That is a crime, and it is never your fault.
• If you are scared right now, call 999. Then tell us - we will believe you and help you move to a safe place. You will not lose your job for telling the truth.

10. Transfer to a new employer
• You can ask to transfer if it really cannot work. Try talking to your employer and to us first.
• You must be paid in full until your last working day, and any salary already owed must still be paid.
• Do not just leave the house - that is "absconding". Talk to us first. But if you are in danger, get to safety first.

If something is wrong, talk to your employer first. If it is not solved, WhatsApp Ming Hwee at +65 6534 2277 - we are here for you.
Other numbers: Police 999, Ambulance 995, MOM 1800-339-5505."""


# Sent instead when our last message WAS the list. The webhook's repeat guard
# (90 s) would drop the same 3,000 characters sent twice, leaving her with no
# reply at all, and resending them unasked would bury what she wrote.
RIGHTS_ALREADY_SENT = (
    "I have sent your full list of rights just above. If something is wrong at "
    "work, tell me what is happening and I will help, or WhatsApp Ming Hwee at "
    "+65 6534 2277. In an emergency call the police on 999."
)

# The list's first line, which is what a transcript shows of it.
RIGHTS_HEADER = HELPER_RIGHTS_MESSAGE.splitlines()[0]


def rights_already_sent(last_bot_line: str) -> bool:
    """Whether our previous message was the detailed rights list."""
    return RIGHTS_HEADER in (last_bot_line or "")


# Vetted at import, like the other strings sent without passing the guards.
assert len(HELPER_RIGHTS_MESSAGE) < 4096, "over WhatsApp's 4,096-character message limit"
assert "8011" not in HELPER_RIGHTS_MESSAGE, "Module 27's old WhatsApp number"
assert "6534 2277" in HELPER_RIGHTS_MESSAGE
assert all(f"\n{n}. " in HELPER_RIGHTS_MESSAGE for n in range(1, 11)), "all ten rights"
assert "**" not in HELPER_RIGHTS_MESSAGE and "#" not in HELPER_RIGHTS_MESSAGE
