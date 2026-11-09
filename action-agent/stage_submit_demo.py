"""
stage_submit_demo.py -- the safety pattern behind the action agent, with a fake portal.

The real agent drives a vendor's web UI with Playwright. Every write is split in two:

    stage(...)  fills the real form, screenshots it, returns a staged_id. Nothing is sent.
    submit(id)  presses the final button, but only if what is in the form right now is
                exactly what the human approved. Anything else refuses.

Rules this file demonstrates:
  * one staged item at a time; any other call cancels it
  * approval is per item and never carries over to the next one
  * submit re-reads the form and refuses if it drifted from the staged text

Run it:  python stage_submit_demo.py          (interactive: approve or skip each message)
         python stage_submit_demo.py --test   (self-check, no input)
"""
import sys
import uuid


class FakePortal:
    """Stands in for the vendor UI: a reply box and an outbox."""

    def __init__(self):
        self.reply_box = ""
        self.outbox = []

    def type_reply(self, text):
        self.reply_box = text

    def press_send(self):
        self.outbox.append(self.reply_box)
        self.reply_box = ""


class Agent:
    def __init__(self, portal):
        self.portal = portal
        self.staged = None  # (staged_id, text): one item at a time

    def stage_message(self, text):
        self.staged = None  # staging anything cancels what was staged before
        self.portal.type_reply(text)
        self.staged = (uuid.uuid4().hex[:8], text)
        return {"staged_id": self.staged[0], "preview": self.portal.reply_box}

    def send_message(self, staged_id):
        if not self.staged or self.staged[0] != staged_id:
            raise RuntimeError("nothing staged under that id: stage it again")
        _, approved = self.staged
        self.staged = None  # an approval is used once
        if self.portal.reply_box != approved:
            raise RuntimeError("the form changed since it was approved: refusing to send")
        self.portal.press_send()
        return {"sent": approved}


def demo():
    portal = FakePortal()
    agent = Agent(portal)
    drafts = [
        "Hi Sam, thanks for applying to the Caregiver role at Willow Bend. Are you free for a call Tuesday?",
        "Hi Priya, just checking in on the start-date paperwork.",
    ]
    for text in drafts:
        staged = agent.stage_message(text)
        print(f"\nSTAGED {staged['staged_id']}:\n  {staged['preview']}")
        if input("  send this one? [y/N] ").strip().lower() == "y":
            print("  ->", agent.send_message(staged["staged_id"]))
        else:
            print("  skipped; nothing sent")
    print("\noutbox:", portal.outbox)


def selftest():
    portal = FakePortal()
    agent = Agent(portal)

    a = agent.stage_message("hello")
    assert portal.outbox == []                       # staging sends nothing
    assert agent.send_message(a["staged_id"]) == {"sent": "hello"}
    try:                                             # approval can't be reused
        agent.send_message(a["staged_id"]); assert False
    except RuntimeError:
        pass

    b = agent.stage_message("first")
    c = agent.stage_message("second")                # staging again cancels b
    try:
        agent.send_message(b["staged_id"]); assert False
    except RuntimeError:
        pass

    portal.type_reply("second, edited by someone else")
    try:                                             # drifted form is refused
        agent.send_message(c["staged_id"]); assert False
    except RuntimeError:
        pass

    assert portal.outbox == ["hello"]
    print("selftest passed")


if __name__ == "__main__":
    selftest() if "--test" in sys.argv else demo()
