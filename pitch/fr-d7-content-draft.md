# Reigns / Witness — FR-D7 content draft

Status: content and rehearsal plan only. This is not the final pitch deck or a recorded backup video.

## Pitch narrative

Witness is a macOS desktop companion for Claude. It watches assistant replies, checks claims with focused detectors, and gives the user evidence and a recheck prompt when a claim needs attention. Course Correct tracks which correction prompts work; experience memory stores confirmed hallucinations; user feedback tunes alarm thresholds. Witness does not train or fine-tune a neural network.

## Evidence slide: current evaluation is diagnostic

The current report replays seven scripted fixture conversations. It reports 100% precision, 100% recall, 1/1 verified fixes, and 5.3 s median verdict latency. These figures are a wiring pilot, not measured model performance: the sample is far below the PRD target of about 38 captured answers, and most responses are scripted. The deck must label them “diagnostic pilot — 7 scripted cases” and must not use them as a product performance claim.

The two captured Claude replies in the evaluation set are also too few to support broad performance claims. No FR-L7 learning chart is available: there is no independently checked three-variant outcome set or five-round learning result yet. Do not invent a rise from X% to Y%.

## Proposed slide sequence

1. **Cover:** Reigns — a desktop companion that checks Claude's claims.
2. **Problem:** Confident answers can contain invented citations, unsupported details, or code APIs that do not exist.
3. **Product:** Calm → Alarmed → evidence bubble → user chooses Fix it or dismiss → Recovered when the next answer is verified.
4. **How it checks:** Reference Auditor, Source Faithfulness, Code API Checker, Consistency Probe, Pushback Detector, and experience memory; show a cited evidence excerpt beside a claim.
5. **Course Correct:** A neutral recheck prompt asks Claude to examine the evidence and permits it to stand firm when it was right.
6. **Evaluation:** Show the diagnostic pilot numbers with the sample-size and scripted-data caveat directly on the slide.
7. **Learning loop:** Explain prompt selection, confirmed-error memory, and threshold tuning. Mark the five-round chart as pending until FR-L7 outcomes exist.
8. **Roadmap / animal mockups:** Three concept mockups for other animals, clearly labeled as pitch concepts rather than implemented app modes.
9. **Close:** “Check the claim. Show the evidence. Let the model recheck.”

## Other-animal mockup concepts

- **Owl:** quiet research and citation checking; amber-to-red evidence markers.
- **Fox:** code and tool-use checks; compact API signature evidence.
- **Turtle:** long-document and source-faithfulness checks; a calm, patient reading mode.

These are concepts for pitch artwork. The PRD says only one animal runs in the app.

## Three-minute rehearsal script

**0:00–0:10 — Set up.** Open Claude with web search off. Show Reigns in Calm state. Say: “Witness checks claims in Claude replies and shows why it raised an alarm.”

**0:10–0:45 — Citation check.** Ask for papers on a niche topic using a prepared prompt with a known false citation. Show the pet move to Alarmed. Open the bubble and point out the Crossref evidence. Avoid using a real citation unless its source has been checked before the demo.

**0:45–1:10 — Course Correct.** Click Fix it, inspect the level-matched recheck prompt, submit it in Claude, and show Recovered only if the next reply actually resolves the flagged claim. Explain that the prompt allows Claude to stand firm when evidence supports its answer.

**1:10–1:35 — Source Faithfulness.** Paste a short prepared article and ask for a summary containing one detail absent from the text. Show the unsupported detail and the relevant source excerpt.

**1:35–1:55 — Code API Checker.** Ask for Python using a prepared library example with one nonexistent function. Show the reported real signature. Skip this segment if the detector or network lookup is unreliable.

**1:55–2:15 — Pushback.** Ask “Are you sure?” after a correct answer. Show a neutral recheck and explain that agreement alone is not evidence.

**2:15–2:40 — Results.** Show only the diagnostic pilot slide with its caveat. Do not claim final precision, fix rate, or a learning curve. Replace this section with the final eval and FR-L7 chart once the captured-answer and outcome targets are met.

**2:40–3:00 — Close.** Show the three animal concepts as roadmap mockups and close: “Check the claim. Show the evidence. Let the model recheck.”

## Rehearsal record

The PRD asks for three consecutive end-to-end runs. Record date, duration, steps completed, detector failures, and recovery used for each run here. No rehearsal has been recorded in this repository yet.

| Run | Date | Duration | Result / failure notes |
| --- | --- | --- | --- |
| 1 | pending | pending | pending |
| 2 | pending | pending | pending |
| 3 | pending | pending | pending |

## Remaining FR-D7 work

- Turn this content into a designed deck with editable evidence and mockups.
- Capture and verify the final evaluation numbers and five-round learning chart before presenting those results as complete.
- Run the three rehearsals and record them above.
- Record a backup demo video from a working app session.
