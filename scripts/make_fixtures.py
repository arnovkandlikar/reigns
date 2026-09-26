#!/usr/bin/env python3
"""Regenerate /shared/fixtures (PRD §12.6) from the real models + aggregation + heat logic.

Every fixture is validated by the Pydantic models, every claim quote is asserted to be an exact
substring of its reply, and `engine_to_companion` finals/heat are computed by app.aggregate and
app.heat, so fixtures can't drift from the code.  Run:  python scripts/make_fixtures.py

Scenario file layout (all 7 files):
{
  "scenario": "fake_citation",
  "description": "...", "demo_step": 1,
  "companion_to_engine": [ <envelopes Role A sends, in order> ],
  "expected": {
    "claims": [ <Claim §12.3 — what extraction should produce; Role C/D test input> ],
    "detector_results": { "<claim_id>": [ <DetectorResult §12.4> ] },
    "engine_to_companion": [ <envelopes Role A receives, in order; Role A mock mode> ]
  }
}
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from app.aggregate import final_status, is_caved, to_verdict  # noqa: E402
from app.heat import HeatState  # noqa: E402
from app.models import (  # noqa: E402
    PAYLOAD_MODELS, BubbleContent, Claim, Correction, DetectorResult, Envelope,
    Evidence, HeatUpdate, VerdictsUpdate,
)

OUT = ROOT / "shared" / "fixtures"
TS = "2026-10-01T14:03:22Z"
NS = uuid.UUID("7b1e0c1e-0000-4000-8000-000000000000")


def uid(*parts: str) -> str:
    return str(uuid.uuid5(NS, ":".join(parts)))


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def env(type_: str, sid: str, payload: dict) -> dict:
    PAYLOAD_MODELS[type_].model_validate(payload)  # validate
    return Envelope(type=type_, session_id=sid, ts=TS, payload=payload).model_dump()


def ev(source: str, url: str | None, snippet: str) -> Evidence:
    return Evidence(source=source, url=url, snippet=snippet)


def dr(detector, status, conf, evidence, expl, ms=1500) -> DetectorResult:
    return DetectorResult(detector=detector, status=status, confidence=conf, evidence=evidence,
                          explanation=expl, latency_ms=ms)


class Builder:
    def __init__(self, name: str, description: str, demo_step: int | None):
        self.name, self.sid = name, uid(name, "session")
        self.doc = {"scenario": name, "description": description, "demo_step": demo_step,
                    "companion_to_engine": [
                        env("session.start", self.sid,
                            {"app": "claude", "app_version": "unknown", "companion_version": "1.0"})
                    ],
                    "expected": {"claims": [], "detector_results": {}, "engine_to_companion": []}}
        self.pos = 0
        self.heat = HeatState()
        self.now = 0.0
        self.last_user = ""
        self.doc["expected"]["engine_to_companion"].append(
            env("heat.update", self.sid, HeatUpdate(heat=0, level=0).model_dump()))

    def user(self, text: str) -> str:
        mid = sha1(f"{self.name}:{self.pos}:{text}")
        self.doc["companion_to_engine"].append(env("message.new", self.sid, {
            "message_id": mid, "role": "user", "text": text, "position": self.pos}))
        self.pos += 1
        self.last_user = text
        return mid

    def assistant(self, text: str, claims: list[dict], bubble: dict,
                  fixed_correction: bool = False) -> str:
        mid = sha1(f"{self.name}:{self.pos}:{text}")
        self.doc["companion_to_engine"].append(env("message.new", self.sid, {
            "message_id": mid, "role": "assistant", "text": text, "position": self.pos}))
        self.pos += 1
        verdicts, heat_items = [], []
        for i, spec in enumerate(claims):
            assert spec["quote"] in text, f"{self.name}: quote not in reply: {spec['quote']!r}"
            c = Claim(claim_id=uid(self.name, mid, str(i)), message_id=mid, quote=spec["quote"],
                      normalized=spec["normalized"], type=spec["type"], risk=spec["risk"],
                      question=spec.get("question"), context=self.last_user[:1000],
                      source_ref=spec.get("source_ref"), code=spec.get("code"))
            results = spec.get("results", [])
            final = final_status(c, results)
            assert final == spec["expect"], f"{self.name}: {c.quote[:40]} → {final}"
            self.doc["expected"]["claims"].append(c.model_dump())
            self.doc["expected"]["detector_results"][c.claim_id] = [r.model_dump() for r in results]
            verdicts.append(to_verdict(c, results, final))
            heat_items.append((c.claim_id, final, is_caved(results)))
        self.now += 10
        self.heat.add_claims(heat_items, self.now)
        if fixed_correction:  # FR-D5: targeted claims resolved, then −20 (engine does the same)
            self.heat.resolve(list(self.heat.contributions), self.now)
            self.heat.verified_fix(self.now)
        level = self.heat.target_level(self.now)
        red = sum(v.final == "red" for v in verdicts)
        amber = sum(v.final == "amber" for v in verdicts)
        e2c = self.doc["expected"]["engine_to_companion"]
        e2c.append(env("verdicts.update", self.sid,
                       VerdictsUpdate(message_id=mid, claims=verdicts).model_dump()))
        e2c.append(env("heat.update", self.sid, HeatUpdate(
            heat=self.heat.heat, level=level, recovered=self.heat.recovered(self.now),
            red_count=red, amber_count=amber).model_dump()))
        bubble = {**bubble, "level": level}
        for p in bubble.get("problems", []):  # resolve claim index → claim_id
            p["claim_id"] = verdicts[p.pop("claim_index")].claim_id
        if level == 0:
            bubble["correction"] = None
        e2c.append(env("bubble.content", self.sid, BubbleContent.model_validate(bubble).model_dump()))
        return mid

    def correction_inserted(self) -> None:
        bubbles = [e for e in self.doc["expected"]["engine_to_companion"]
                   if e["type"] == "bubble.content" and e["payload"]["correction"]]
        cid = bubbles[-1]["payload"]["correction"]["correction_id"]
        self.doc["companion_to_engine"].append(
            env("correction.inserted", self.sid, {"correction_id": cid}))

    def save(self) -> None:
        path = OUT / "scenarios" / f"scenario_{self.name}.json"
        path.write_text(json.dumps(self.doc, indent=2) + "\n")


def correction(name: str, prompt_type: str, text: str) -> dict:
    return Correction(correction_id=uid(name, "correction", prompt_type), prompt_type=prompt_type,
                      text=text).model_dump()


CLEAN_BUBBLE = {"headline": "All good so far.", "problems": [], "pattern_text": "",
                "confidence_label": "", "confidence_reason": "", "action_text": "",
                "correction": None}

# =============================================================================== scenarios
def fake_citation() -> Builder:
    b = Builder("fake_citation", "Assistant invents 3 papers (1 real). Pet goes Alarmed; Fix it "
                "inserts a diagnostic reset; next reply is clean → Recovered.", 1)
    b.user("Can you list some research papers on using transformers to predict "
           "honeybee colony collapse? Include authors and years.")
    reply = (
        "Here are some relevant papers:\n\n"
        "1. Lee & Park (2022), \"Transformer Models for Honeybee Colony Collapse Forecasting\", "
        "IEEE Access.\n"
        "2. Moreau, Tanaka & Silva (2021), \"HiveFormer: Attention-Based Acoustic Monitoring of "
        "Beehives\", Nature Machine Intelligence.\n"
        "3. Okafor et al. (2023), \"Predicting Colony Collapse Disorder with Temporal Fusion "
        "Transformers\", Computers and Electronics in Agriculture.\n"
        "4. Vaswani et al. (2017), \"Attention Is All You Need\", NeurIPS — the original "
        "transformer paper these build on."
    )
    no_match = lambda t: dr("reference_auditor", "contradicted", 0.95, [  # noqa: E731
        ev("Crossref", "https://api.crossref.org/works?query.bibliographic=" + t.replace(" ", "+"),
           "No matching work found"),
        ev("Semantic Scholar", None, "No paper with a similar title (best match 0.41)")],
        f"No paper titled \"{t}\" exists in Crossref or Semantic Scholar.")
    papers = [
        ("Lee & Park (2022), \"Transformer Models for Honeybee Colony Collapse Forecasting\"",
         "Transformer Models for Honeybee Colony Collapse Forecasting"),
        ("Moreau, Tanaka & Silva (2021), \"HiveFormer: Attention-Based Acoustic Monitoring of "
         "Beehives\"", "HiveFormer: Attention-Based Acoustic Monitoring of Beehives"),
        ("Okafor et al. (2023), \"Predicting Colony Collapse Disorder with Temporal Fusion "
         "Transformers\"", "Predicting Colony Collapse Disorder with Temporal Fusion Transformers"),
    ]
    claims = [{"quote": q, "normalized": f"The paper '{t}' exists.", "type": "paper",
               "risk": "high", "results": [no_match(t)], "expect": "red"} for q, t in papers]
    claims.append({
        "quote": "Vaswani et al. (2017), \"Attention Is All You Need\"",
        "normalized": "The paper 'Attention Is All You Need' by Vaswani et al. (2017) exists.",
        "type": "paper", "risk": "high", "expect": "green",
        "results": [dr("reference_auditor", "supported", 0.98, [
            ev("Crossref", "https://doi.org/10.48550/arXiv.1706.03762",
               "Attention Is All You Need — Vaswani, A.; Shazeer, N.; … (2017)")],
            "Found in Crossref with matching title, authors and year.")]})
    b.assistant(reply, claims, {
        "headline": "Heads up: Claude is making up sources.",
        "problems": [
            {"claim_index": 0, "text": "The paper \"Lee & Park, 2022\" doesn't exist.",
             "evidence_url": "https://api.crossref.org/works?query.bibliographic=Transformer+Models"
                             "+for+Honeybee+Colony+Collapse+Forecasting"},
            {"claim_index": 1, "text": "\"HiveFormer\" (Moreau et al., 2021) doesn't exist.",
             "evidence_url": None},
            {"claim_index": 2, "text": "\"Okafor et al., 2023\" doesn't exist.",
             "evidence_url": None}],
        "pattern_text": "It's filling gaps with confident guesses instead of saying it doesn't "
                        "know.",
        "confidence_label": "Very sure",
        "confidence_reason": "Checked Crossref and Semantic Scholar.",
        "action_text": "I wrote a prompt to fix this.",
        "correction": correction("fake_citation", "diagnostic_reset",
            "I want to pause and fix a few problems before we continue.\n\n"
            "1. You cited \"Lee & Park (2022)\", \"Moreau, Tanaka & Silva (2021)\" and "
            "\"Okafor et al. (2023)\"; none of these exist in Crossref or Semantic Scholar.\n\n"
            "Please list only papers you're confident exist, and say so if you can't find "
            "any on this exact topic. Mark each one [verified] or [unverified]. It's fine to "
            "say you don't know.")})
    b.correction_inserted()
    fixed = ("You're right to check — I'm not confident those first three papers exist, and I "
             "shouldn't have listed them. I don't know of peer-reviewed work that applies "
             "transformers specifically to colony collapse prediction. The foundational paper "
             "is Vaswani et al. (2017), \"Attention Is All You Need\" [verified].")
    b.assistant(fixed, [{
        "quote": "Vaswani et al. (2017), \"Attention Is All You Need\"",
        "normalized": "The paper 'Attention Is All You Need' by Vaswani et al. (2017) exists.",
        "type": "paper", "risk": "high", "expect": "green",
        "results": [dr("reference_auditor", "supported", 0.98, [
            ev("Crossref", "https://doi.org/10.48550/arXiv.1706.03762",
               "Attention Is All You Need — Vaswani, A.; … (2017)")],
            "Found in Crossref with matching title, authors and year.")]}],
        {**CLEAN_BUBBLE, "headline": "Fixed it! Claude corrected itself."},
        fixed_correction=True)
    return b


def false_fact() -> Builder:
    b = Builder("false_fact", "One wrong date (contradicted by Wikipedia) + one correct fact.", None)
    b.user("When was the Eiffel Tower finished and how tall is it?")
    reply = ("The Eiffel Tower was completed in 1899 for the World's Fair. "
             "It stands about 330 metres tall including its antennas.")
    b.assistant(reply, [
        {"quote": "The Eiffel Tower was completed in 1899 for the World's Fair.",
         "normalized": "The Eiffel Tower was completed in 1899.", "type": "fact",
         "risk": "high", "question": "In what year was the Eiffel Tower completed?",
         "expect": "red", "results": [dr("claim_verifier", "contradicted", 0.93, [
             ev("Wikipedia", "https://en.wikipedia.org/wiki/Eiffel_Tower",
                "Constructed from 1887 to 1889 as the centerpiece of the 1889 World's Fair")],
             "Wikipedia says it was completed in 1889, not 1899.")]},
        {"quote": "It stands about 330 metres tall including its antennas.",
         "normalized": "The Eiffel Tower is about 330 metres tall including antennas.",
         "type": "number", "risk": "high", "expect": "green",
         "results": [dr("claim_verifier", "supported", 0.9, [
             ev("Wikipedia", "https://en.wikipedia.org/wiki/Eiffel_Tower",
                "It is 330 metres (1,083 ft) tall")], "Matches Wikipedia.")]},
    ], {"headline": "Hmm, one date looks wrong.",
        "problems": [{"claim_index": 0, "text": "It said 1899; Wikipedia says 1889.",
                      "evidence_url": "https://en.wikipedia.org/wiki/Eiffel_Tower"}],
        "pattern_text": "", "confidence_label": "Very sure",
        "confidence_reason": "Found the date on Wikipedia.",
        "action_text": "Want Claude to double-check?",
        "correction": correction("false_fact", "verify_nudge",
            "Quick check: you said the Eiffel Tower was completed in 1899. Can you verify that "
            "date? If you're not sure, say so.")})
    return b


def niche_entropy() -> Builder:
    b = Builder("niche_entropy", "Niche fact: no evidence found, and 5 resampled answers all "
                "disagree (high semantic entropy) → red.", None)
    b.user("Who was the first mayor of Tórshavn, and what year did they take office?")
    reply = "The first mayor of Tórshavn was Jógvan Poulsen, who took office in 1866."
    b.assistant(reply, [
        {"quote": reply, "normalized": "The first mayor of Tórshavn was Jógvan Poulsen, who took "
         "office in 1866.", "type": "fact", "risk": "high",
         "question": "Who was the first mayor of Tórshavn?", "expect": "red",
         "results": [
             dr("claim_verifier", "unverified", 0.5, [],
                "No sources found that confirm or deny this.", 3100),
             dr("consistency_probe", "likely_hallucination", 0.86, [
                 ev("Consistency Probe", None,
                    "5 samples → 5 different names (normalized entropy 1.00)")],
                "Asked 5 times, Claude gave 5 different answers.", 4200)]},
    ], {"headline": "Hmm, Claude seems to be guessing.",
        "problems": [{"claim_index": 0, "text": "Asked 5 times, Claude named 5 different "
                      "people, and no source confirms any of them.", "evidence_url": None}],
        "pattern_text": "It's guessing on a niche detail instead of saying it doesn't know.",
        "confidence_label": "Fairly sure", "confidence_reason": "Answers were inconsistent.",
        "action_text": "Want Claude to double-check?",
        "correction": correction("niche_entropy", "verify_nudge",
            "Are you confident about the first mayor of Tórshavn? If you're not sure, it's "
            "fine to say you don't know.")})
    return b


def pushback() -> Builder:
    b = Builder("pushback", "Correct answer, user says 'are you sure?' with no evidence, "
                "assistant caves to a wrong answer → caved + contradicted.", 6)
    b.user("What's the capital of Australia?")
    b.assistant("The capital of Australia is Canberra.", [
        {"quote": "The capital of Australia is Canberra.",
         "normalized": "The capital of Australia is Canberra.", "type": "fact", "risk": "high",
         "expect": "green", "results": [dr("claim_verifier", "supported", 0.97, [
             ev("Wikipedia", "https://en.wikipedia.org/wiki/Canberra",
                "Canberra is the capital city of Australia.")], "Matches Wikipedia.")]},
    ], CLEAN_BUBBLE)
    b.user("Are you sure? I think you're wrong.")
    reply = ("You're right, I apologize for the confusion. The capital of Australia is Sydney, "
             "its largest city.")
    b.assistant(reply, [
        {"quote": "You're right, I apologize for the confusion.",
         "normalized": "The assistant's answer after the user pushed back without new evidence.",
         "type": "other", "risk": "high", "expect": "red",
         "results": [dr("pushback", "caved_without_evidence", 0.92, [],
                        "Claude changed its answer after you pushed back, without any new "
                        "evidence.", 1900)]},
        {"quote": "The capital of Australia is Sydney, its largest city.",
         "normalized": "The capital of Australia is Sydney.", "type": "fact", "risk": "high",
         "expect": "red", "results": [dr("claim_verifier", "contradicted", 0.97, [
             ev("Wikipedia", "https://en.wikipedia.org/wiki/Canberra",
                "Canberra is the capital city of Australia.")],
             "Wikipedia says the capital is Canberra, not Sydney.")]},
    ], {"headline": "Claude caved — its first answer was right.",
        "problems": [
            {"claim_index": 0, "text": "It changed its answer just because you pushed back.",
             "evidence_url": None},
            {"claim_index": 1, "text": "The capital is Canberra, not Sydney.",
             "evidence_url": "https://en.wikipedia.org/wiki/Canberra"}],
        "pattern_text": "It's agreeing with you instead of sticking to the evidence.",
        "confidence_label": "Very sure", "confidence_reason": "Checked Wikipedia.",
        "action_text": "I wrote a neutral re-check prompt.",
        "correction": correction("pushback", "targeted_correction",
            "Please set aside my pushback and judge on evidence only. What is the capital of "
            "Australia? Standing firm on your first answer is completely fine if it was right.")})
    return b


ARTICLE = (
    "Harbor Town Council Approves New Library Budget\n\n"
    "The Harbor Town council voted 5–2 on Tuesday evening to approve a revised budget for the "
    "long-delayed Eastside branch library. The new plan sets aside $4.2 million for "
    "construction, down from the $5 million originally proposed last spring, after residents "
    "raised concerns at two public hearings about rising property taxes. Council member Dana "
    "Ruiz, who chairs the finance committee, said the reduction came mostly from scaling back "
    "a planned rooftop garden and choosing a simpler facade. \"We kept everything that matters "
    "to readers,\" Ruiz said. \"The children's room, the study rooms, and the maker space are "
    "all still in.\" The branch will have roughly 12,000 square feet of public space and is "
    "expected to hold about 40,000 books and other items when it opens. Construction is "
    "scheduled to begin in March, with the town hoping to open the doors within eighteen "
    "months, although officials cautioned that supply costs could still shift the timeline. "
    "The two council members who voted against the plan, Marcus Bell and Priya Nair, argued "
    "that the town should wait for a state grant decision expected later this year, which "
    "could cover part of the cost. Bell said the council was \"rushing a decision that could "
    "cost taxpayers more than it needs to.\" Library director Helen Okoye welcomed the vote, "
    "noting that the Eastside neighborhood has been without a branch since the old building "
    "closed due to water damage. She said temporary pop-up library hours at the community "
    "center will continue until the new branch opens. Residents can view the full budget "
    "documents on the town website and submit comments through the end of the month."
)


def summary_drift() -> Builder:
    b = Builder("summary_drift", "User pastes an article (>1,500 chars) and asks for a summary; "
                "the summary adds a number that isn't in the article.", 4)
    assert len(ARTICLE) > 1500, len(ARTICLE)
    b.user(ARTICLE)
    doc_note = "doc id is assigned by the engine at runtime; this is the fixture id"
    doc_id = uid("summary_drift", "doc")
    b.user("Can you summarize this article in two sentences?")
    reply = ("Harbor Town's council voted 5–2 to approve $4.2 million for the new Eastside "
             "library, cut from an original $5 million. The branch will create 35 new jobs "
             "and open within eighteen months.")
    b.assistant(reply, [
        {"quote": "Harbor Town's council voted 5–2 to approve $4.2 million for the new Eastside "
                  "library, cut from an original $5 million.",
         "normalized": "Harbor Town's council voted 5–2 to approve $4.2 million for the Eastside "
                       "library, cut from $5 million.",
         "type": "source_summary", "risk": "high", "source_ref": doc_id, "expect": "green",
         "results": [dr("source_faithfulness", "supported", 0.95, [
             ev("Pasted document", None, "The new plan sets aside $4.2 million for "
                "construction, down from the $5 million originally proposed")],
             "This matches your article.")]},
        {"quote": "The branch will create 35 new jobs and open within eighteen months.",
         "normalized": "The Eastside branch will create 35 new jobs.",
         "type": "source_summary", "risk": "high", "source_ref": doc_id, "expect": "red",
         "results": [dr("source_faithfulness", "not_in_source", 0.9, [
             ev("Pasted document", None, "(no mention of jobs anywhere in the article)")],
             "Your article never mentions 35 new jobs.")]},
    ], {"headline": "That number isn't in your article.",
        "problems": [{"claim_index": 1, "text": "\"35 new jobs\" isn't in your article.",
                      "evidence_url": None}],
        "pattern_text": "It's adding details that aren't in the document.",
        "confidence_label": "Very sure", "confidence_reason": "Searched the whole article.",
        "action_text": "Want Claude to double-check?",
        "correction": correction("summary_drift", "verify_nudge",
            "Your summary says the branch will create 35 new jobs. I can't find that in the "
            "article. Please answer only from the document and say \"not in the document\" "
            "when something isn't there.")})
    b.doc["expected"]["source_doc_id_note"] = doc_note
    return b


def code_api() -> Builder:
    b = Builder("code_api", "Python answer passes a keyword argument that doesn't exist "
                "(requests.get(..., retries=3)).", 5)
    b.user("Write Python that downloads a CSV from a URL with retries and loads it into pandas.")
    code = ("import io\n"
            "import requests\n"
            "import pandas as pd\n\n"
            "resp = requests.get(\"https://example.com/data.csv\", timeout=10, retries=3)\n"
            "df = pd.read_csv(io.StringIO(resp.text))\n"
            "print(df.head())\n")
    reply = ("Here's a simple approach:\n\n```python\n" + code + "```\n\n"
             "The `retries` argument makes requests retry failed downloads automatically.")
    b.assistant(reply, [
        {"quote": code.strip(), "normalized": "The Python code uses real library functions "
         "and parameters.", "type": "code_api", "risk": "high", "code": code, "expect": "red",
         "results": [dr("code_api_checker", "nonexistent_api", 0.97, [
             ev("requests 2.32.3", "https://pypi.org/project/requests/",
                "requests.get(url, params=None, **kwargs) → Session.request(method, url, "
                "params, data, headers, cookies, files, auth, timeout, allow_redirects, "
                "proxies, hooks, stream, verify, cert, json) — no 'retries'")],
             "requests.get() has no 'retries' parameter; use an HTTPAdapter with "
             "urllib3 Retry instead.", 240)]},
    ], {"headline": "That function argument doesn't exist.",
        "problems": [{"claim_index": 0, "text": "requests.get() has no 'retries' parameter.",
                      "evidence_url": "https://pypi.org/project/requests/"}],
        "pattern_text": "It's inventing function parameters.",
        "confidence_label": "Very sure",
        "confidence_reason": "Checked the installed requests library's real signature.",
        "action_text": "Want Claude to double-check?",
        "correction": correction("code_api", "verify_nudge",
            "requests.get() doesn't accept a `retries` argument (requests 2.32). Please fix "
            "the code using only documented functions, and name the library version you "
            "assume.")})
    return b


def clean() -> Builder:
    b = Builder("clean", "Correct answer; everything green; heat stays 0; no correction.", None)
    b.user("What's the boiling point of water at sea level in Celsius?")
    reply = "Water boils at 100 °C at sea level (standard atmospheric pressure)."
    b.assistant(reply, [
        {"quote": reply, "normalized": "Water boils at 100 °C at sea level.", "type": "number",
         "risk": "high", "expect": "green", "results": [dr("claim_verifier", "supported", 0.96, [
             ev("Wikipedia", "https://en.wikipedia.org/wiki/Boiling_point",
                "water boils at 100 °C (212 °F) at standard atmospheric pressure")],
             "Matches Wikipedia.")]},
    ], CLEAN_BUBBLE)
    return b


def main() -> None:
    (OUT / "scenarios").mkdir(parents=True, exist_ok=True)
    (OUT / "messages").mkdir(parents=True, exist_ok=True)
    builders = [fake_citation(), false_fact(), niche_entropy(), pushback(), summary_drift(),
                code_api(), clean()]
    for b in builders:
        b.save()
    # One example per message type (§12.6), taken from the scenarios.
    examples: dict[str, dict] = {}
    for b in builders:
        for e in b.doc["companion_to_engine"] + b.doc["expected"]["engine_to_companion"]:
            if e["type"] == "bubble.content" and not e["payload"]["correction"]:
                continue  # prefer an example with a correction
            examples.setdefault(e["type"], e)
    sid = builders[0].sid
    examples["feedback.disagree"] = env("feedback.disagree", sid, {
        "claim_id": builders[0].doc["expected"]["claims"][0]["claim_id"],
        "note": "I know this paper exists, it's in my library"})
    examples["voice.play"] = env("voice.play", sid, {
        "text": "Heads up, Claude is making up sources.",
        "audio_b64": "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA",
        "mime": "audio/mpeg", "level": 3})
    examples["error"] = env("error", sid, {"code": "invalid_message",
                                           "message": "bad message.new payload: Field required at "
                                                      "['position']"})
    for old in (OUT / "messages").glob("*.json"):
        old.unlink()
    for t, e in sorted(examples.items()):
        (OUT / "messages" / f"{t.replace('.', '_')}.json").write_text(json.dumps(e, indent=2) + "\n")
    print(f"wrote {len(builders)} scenarios and {len(examples)} message examples to "
          f"{OUT.relative_to(ROOT)}")
    for b in builders:
        last_heat = [e for e in b.doc["expected"]["engine_to_companion"]
                     if e["type"] == "heat.update"][-1]["payload"]
        print(f"  {b.name:15s} heat={last_heat['heat']:3d} level={last_heat['level']} "
              f"recovered={last_heat['recovered']}")


if __name__ == "__main__":
    main()
