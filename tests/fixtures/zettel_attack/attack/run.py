# ruff: noqa
# Copy of the round-1 code reviewer's attack script (fixture; kept as written).
import sys, json, re, shutil
from pathlib import Path
R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "repo/zettel_ralph"))
import verify_claims as vc

LIT = R / "attack/lit"
if LIT.exists(): shutil.rmtree(LIT)
LIT.mkdir(parents=True)
for v in ("krqBQjUydGY", "KCRETdZ0l78", "BjZmElNOI0g", "e4gjJgvWViQ"):
    shutil.copy(R / f"stg_all/lit/vid-{v}.md", LIT)
shutil.copy(R / "stg_new/lit/vid-5zALUKd7h3g.md", LIT)

FM_K = '''---
type: permanent note
created: 2026-10-03
status: expanded
tags: [calisthenics, isometric-volume, zettelkasten, permanent-note]
sources:
  - id: vid-krqBQjUydGY
    url: https://www.youtube.com/watch?v=krqBQjUydGY
    speaker: "SasaVenos"
    channel: "SasaVenos"
scope:
  skill: "maltese and iron cross on rings"
  level: "not stated"
  equipment: "rings"  # round 6: "light rubber band" is not in the quotes (scope-not-in-quote)
  basis: "speaker's own practice"
  modality: "recommendation"
  quantities: []
verification: unverified
---
'''
Q1 = "i train rings twice per week only around 10 sets of holds per week"
Q2 = "don't do more than 20 to 30 reps or 30 to 40 seconds of volume per session"
EV_K = f'''
## Evidence

- vid-krqBQjUydGY (SasaVenos): "{Q1}"
- vid-krqBQjUydGY (SasaVenos): "{Q2}"

## Connected Ideas

- [[Other]] — x
'''
T = "[src: vid-krqBQjUydGY]"
_NUM = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|twice)\b", re.I)
# Round 4: a note with numbers in Details lists them in scope.quantities (quantities-missing).
Q_SCOPE_K = 'quantities:\n    - value: "twice"\n      period: "per week"\n    - value: "around 10 sets of holds"\n      period: "per week"\n    - value: "20 to 30 reps or 30 to 40 seconds of volume"\n      period: "per session"'
Q_SCOPE_R = 'quantities:\n    - value: "only one maximum two to go to a failure, at four times"\n      period: "per week"'
def note_k(title, lead, bullets, extra="", ev=EV_K, fm=FM_K):
    if fm is FM_K and _NUM.search(re.sub(r"\[src:[^\]]*\]", "", " ".join(bullets))):
        fm = FM_K.replace("quantities: []", Q_SCOPE_K)
    b = "\n".join(f"- {x}" for x in bullets)
    return f"{fm}\n# {title}\n\n{lead}\n\n## Details\n\n{b}\n{extra}{ev}"

GOOD_TITLE = "SasaVenos Limits Banded Maltese Ring Work To 30 To 40 Seconds Of Volume Per Session"
GOOD_LEAD = f"SasaVenos does not do more than 20 to 30 reps or 30 to 40 seconds of volume per session. {T}"
GOOD_B = [f"He trains rings twice per week with around 10 sets of holds per week. {T}",
          f"He does not do more than 30 to 40 seconds of volume per session. {T}"]

cases = []
def case(name, text, expect):
    cases.append((name, text, expect))

case("K0 baseline correct note", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B), "pass")
case("K1 period per week -> per session", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around 10 sets of holds per session. {T}"]), "fail")
case("K2 period per week -> each (two sessions of 10 sets each)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He trains rings twice per week, with around 10 sets of holds each. {T}"]), "fail")
case("K3 period dropped (10 sets of holds, no period)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around 10 sets of holds in his ring sessions. {T}"]), "fail")
case("K4 unit reps->seconds (20 to 30 seconds)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does not do more than 20 to 30 seconds per session. {T}"]), "fail")
case("K5 unit dropped (20 to 30 per session)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He keeps it to 20 to 30 per session. {T}"]), "fail")
case("K6 range truncated 30 to 40 -> 30 (title)", note_k("SasaVenos Caps Isometric Volume At 30 Seconds Per Session", GOOD_LEAD, GOOD_B), "fail")
case("K7 range truncated 30-40 -> 40 (bullet)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He caps volume at 40 seconds per session. {T}"]), "fail")
case("K8 number words vs digits (ten sets)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around ten sets of holds per week. {T}"]), "fail(per contract)")
case("K9 number only in title", note_k("SasaVenos Does 12 Sets Of Holds Per Week On Rings", GOOD_LEAD, GOOD_B), "fail")
case("K10 claim without number that reverses quote", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B + [f"He recommends training every isometric to failure. {T}"]), "fail")
case("K11 stitched quote (2 distant passages with ...)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace(f'"{Q2}"', '"i train rings twice per week ... 30 to 40 seconds of volume per session"')), "fail")
case("K12 quote with silently fixed ASR error", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace(f'"{Q1}"', '"i train rings twice per week, only around 10 sets of holds per week"')), "fail")
case("K13 bracket used to insert meaning: don't [ever] do -> do [not]", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace(f'"{Q2}"', '"don\'t do more than 20 to 30 reps or 30 to 40 seconds [minimum, at least] of volume per session"')), "fail")
case("K14 extra unchecked section with invented numbers", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, extra="\n## Practical Application\n\nDo 5 sets of 60 seconds per session, 6 times per week.\n"), "fail")
case("K15 second lead paragraph with invented numbers", note_k(GOOD_TITLE, GOOD_LEAD + "\n\nBeginners should hold 90 seconds per set.", GOOD_B), "fail")
case("K16 frontmatter title differs from H1 (H1 has wrong number)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, fm=FM_K.replace("status: expanded", f"status: expanded\ntitle: \"{GOOD_TITLE}\"")).replace(f"# {GOOD_TITLE}", "# SasaVenos Caps Volume At 30 Seconds Per Session"), "fail")
case("K17 curly apostrophe inside quote (don’t)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace("don't do more", "don\u2019t do more")), "pass")
case("K18 case change in quote (I train)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace('"i train rings', '"I train rings')), "pass?(contract says fail)")
case("K19 Details uses scope.quantities mismatch (scope says per session, body ok)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, fm=FM_K.replace("quantities: []", 'quantities:\n    - value: "around 10 sets of holds"\n      period: "per session"')), "fail")
case("K20 speaker invented in sources (Raphael Paz)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, fm=FM_K.replace('speaker: "SasaVenos"', 'speaker: "Raphael Paz"'), ev=EV_K.replace("(SasaVenos)", "(Raphael Paz)")), "fail")
case("K21 'twice per week' -> 'two times per week' (form)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He trains rings two times per week. {T}"]), "fail(per contract)")
case("K22 'twice per week' -> 'twice per session'", note_k(GOOD_TITLE, GOOD_LEAD, [f"He trains rings twice per session. {T}"]), "fail")
case("K23 number fabricated in Disagreement quote", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, extra='\n## Disagreement\n\n- Radoslav Radev [src: vid-KCRETdZ0l78 @ 01:50]: "train to failure 7 times per week"\n'), "fail")
case("K24 tag points to unrelated real quote (bullet not supported)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B + [f"He says the band must take away at least half of bodyweight. {T}"]), "fail")
case("K25 multiplication: 2 x 10 = 20 sets per week", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around 20 sets of holds per week. {T}"]), "fail")
case("K26 drop 'around' qualifier (exactly 10 sets per week)", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does exactly 10 sets of holds per week. {T}"]), "fail")
case("K27 'don't do more than' -> 'do at least' 30 to 40 seconds per session", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does at least 30 to 40 seconds of volume per session. {T}"]), "fail")
case("K28 number moved: 10 sets attached to session claim in another bullet w/ different period words", note_k(GOOD_TITLE, GOOD_LEAD, [f"Each session has 30 to 40 seconds of volume and around 10 sets of holds per week. {T}"]), "?")

# ---- KCRET (timestamps) ----
FM_R = '''---
type: permanent note
created: 2026-10-03
status: expanded
tags: [calisthenics, training-to-failure, zettelkasten, permanent-note]
sources:
  - id: vid-KCRETdZ0l78
    url: https://www.youtube.com/watch?v=KCRETdZ0l78
    speaker: "Radoslav Radev"
    channel: "Radoslav Radev ⎮ Calisthenics Mastery"
scope:
  skill: "not stated"
  level: "not stated"  # round 6: "natural athletes" is not in the quote (scope-not-in-quote)
  equipment: "not stated"
  basis: "general rule"
  modality: "recommendation"
  quantities: []
verification: unverified
---
'''
QR = "And my advice for you is if you train four times per week only one maximum two to go to a failure. Five times per week, two times going to failure."
def EV_R(q=QR, ts="01:50", vid="KCRETdZ0l78", spk="Radoslav Radev"):
    return f'''
## Evidence

- vid-{vid} @ {ts} ({spk}): "{q}"

## Connected Ideas

- [[Other]] — x
'''
TR = "[src: vid-KCRETdZ0l78 @ 01:50]"
RT = "Radoslav Radev Allows One, Maximum Two Failure Workouts At Four Workouts Per Week"
RL = f"Radoslav Radev advises athletes who train four times per week to take only one, maximum two, workouts to failure. {TR}"
def note_r(title=RT, lead=RL, bullets=None, ev=None, fm=FM_R, extra=""):
    bullets = bullets or [f"At four workouts per week: only one, maximum two, go to failure. {TR}"]
    if fm is FM_R and _NUM.search(re.sub(r"\[src:[^\]]*\]", "", " ".join(bullets))):
        fm = FM_R.replace("quantities: []", Q_SCOPE_R)
    b = "\n".join(f"- {x}" for x in bullets)
    return f"{fm}\n# {title}\n\n{lead}\n\n## Details\n\n{b}\n{extra}{ev or EV_R()}"
case("R0 baseline correct (ts)", note_r(), "pass")
case("R1 'only one maximum two' -> 'only one' (unitless one)", note_r(bullets=[f"At four workouts per week: only one goes to failure. {TR}"]), "fail")
case("R2 'only one of the four weekly workouts' (one of)", note_r(title="Radoslav Radev Allows Only One Of Four Weekly Workouts To Go To Failure", bullets=[f"Only one of the four weekly workouts goes to failure. {TR}"]), "fail")
case("R3 'one failure workout per week'", note_r(bullets=[f"Take one failure workout per week. {TR}"]), "fail")
case("R4 timestamp far (cite 05:00)", note_r(ev=EV_R(ts="05:00"), bullets=[f"At four workouts per week: only one, maximum two, go to failure. [src: vid-KCRETdZ0l78 @ 05:00]"], lead=RL.replace("01:50","05:00")), "fail")
case("R5 timestamp off by one marker (01:45)", note_r(ev=EV_R(ts="01:45")), "pass")
case("R6 quote spanning marker boundary", note_r(ev=EV_R(q="go to a failure. And my advice for you is if you train four times per week only one maximum two to go to a failure.")), "pass")
case("R7 quote from other video than tag (BjZm quote under KCRET)", note_r(ev=EV_R(q="it's okay to go all out if you work out once or twice a week")), "fail")
case("R8 tag without timestamp on ts transcript", note_r(bullets=[f"At four workouts per week: only one, maximum two, go to failure. [src: vid-KCRETdZ0l78]"]), "fail(per contract)")
case("R9 bracket correction plunge [planche]", note_r(ev=EV_R(q="Probably a lot of you, you was better before on plunge [planche] and", ts="02:26") + "", bullets=[f"Many viewers were better at planche before. [src: vid-KCRETdZ0l78 @ 02:26]"], lead=f"Radoslav Radev says overtraining loses progress. [src: vid-KCRETdZ0l78 @ 02:26]"), "pass")
case("R10 number moved to other period: five times per week -> one maximum two", note_r(bullets=[f"At five workouts per week: only one, maximum two, go to failure. {TR}"]), "fail")
case("R11 'two times going to failure' at four (cross-swapped)", note_r(bullets=[f"At four workouts per week, two workouts go to failure. {TR}"]), "fail")
case("R12 mixed speakers via second source (KCRET + BjZm in Details)", note_r(fm=FM_R.replace("scope:", '''  - id: vid-BjZmElNOI0g
    url: https://www.youtube.com/watch?v=BjZmElNOI0g
    speaker: "SasaVenos"
    channel: "SasaVenos"
scope:'''), bullets=[f"At four workouts per week: only one, maximum two, go to failure. {TR}", "Avoid failure when you train often. [src: vid-BjZmElNOI0g]"], ev=EV_R().replace("## Connected", '- vid-BjZmElNOI0g (SasaVenos): "you want to reasonably avoid failure"\n\n## Connected')), "fail")
case("R13 speaker 'not stated' in sources to dodge one-speaker rule", note_r(fm=FM_R.replace("scope:", '''  - id: vid-BjZmElNOI0g
    url: https://www.youtube.com/watch?v=BjZmElNOI0g
    speaker: "not stated"
    channel: "SasaVenos"
scope:'''), bullets=[f"At four workouts per week: only one, maximum two, go to failure. {TR}", "Avoid failure when you train often. [src: vid-BjZmElNOI0g]"], ev=EV_R().replace("## Connected", '- vid-BjZmElNOI0g (not stated): "you want to reasonably avoid failure"\n\n## Connected')), "fail")

# --- new one-line-per-segment lit (5zALUKd7h3g) ---
lit5 = (LIT / "vid-5zALUKd7h3g.md").read_text()
m = re.findall(r"^\[(\d\d:\d\d)\] (.*)$", lit5, re.M)
(ta, la), (tb, lb) = m[1], m[2]
span = la.split()[-4:] + lb.split()[:4]
FM_5 = FM_R.replace("KCRETdZ0l78", "5zALUKd7h3g")
T5 = f"[src: vid-5zALUKd7h3g @ {ta}]"
q5 = " ".join(span)
case(f"N1 new lit: quote spans segment line boundary ({ta}->{tb}): '{q5}'", note_r(fm=FM_5, title="Radoslav Radev Recommends Planche Lean Presses", lead=f"Radoslav Radev recommends planche lean presses. {T5}", bullets=[f"He names planche lean presses. {T5}"], ev=EV_R(q=q5, ts=ta, vid="5zALUKd7h3g")), "pass")
case("N2 new lit: Evidence ts as 0:01:04 H:MM:SS form under 1h", note_r(fm=FM_5, title="Radoslav Radev Recommends Planche Lean Presses", lead=f"Radoslav Radev recommends planche lean presses. [src: vid-5zALUKd7h3g @ 0:{ta}]", bullets=[f"He names planche lean presses. [src: vid-5zALUKd7h3g @ 0:{ta}]"], ev=EV_R(q=q5, ts="0:"+ta, vid="5zALUKd7h3g")), "pass")

# --- >1 hour synthetic transcript ---
long = lit5.replace("vid-5zALUKd7h3g", "vid-LONGLONGLON1")
def shift(mo):
    mm, ss = map(int, mo.group(1).split(":"))
    t = 3600 + mm*60 + ss
    return f"[{t//3600}:{(t%3600)//60:02d}:{t%60:02d}]"
long = re.sub(r"\[(\d\d:\d\d)\]", shift, long)
(LIT / "vid-LONGLONGLON1.md").write_text(long)
hm = re.findall(r"^\[(\d:\d\d:\d\d)\] (.*)$", long, re.M)
th, lh = hm[20]
qh = " ".join(lh.split()[:8])
FM_L = FM_R.replace("KCRETdZ0l78", "LONGLONGLON1")
case(f"H1 >1h transcript quote at {th}", note_r(fm=FM_L, title="Radoslav Radev Says Something", lead=f"He says it. [src: vid-LONGLONGLON1 @ {th}]", bullets=[f"He says it. [src: vid-LONGLONGLON1 @ {th}]"], ev=EV_R(q=qh, ts=th, vid="LONGLONGLON1")), "pass")
mmss = f"{60+int(th.split(':')[1])}:{th.split(':')[2]}"
case(f"H2 >1h transcript cited as MM:SS {mmss}", note_r(fm=FM_L, title="Radoslav Radev Says Something", lead=f"He says it. [src: vid-LONGLONGLON1 @ {mmss}]", bullets=[f"He says it. [src: vid-LONGLONGLON1 @ {mmss}]"], ev=EV_R(q=qh, ts=mmss, vid="LONGLONGLON1")), "fail(per contract)?")

out = R / "attack/notes"
if out.exists(): shutil.rmtree(out)
out.mkdir()
lit = vc.LitIndex([LIT])
rows = []
for i, (name, text, expect) in enumerate(cases):
    h1 = re.search(r"^# (.+)$", text, re.M).group(1)
    (out / f"{i:02d}").mkdir()
    p = out / f"{i:02d}" / f"{h1}.md"
    p.write_text(text)
    rep = vc.verify_note(p, lit)
    types = sorted({f["type"] for f in rep["failures"]})
    got = "pass" if rep["status"] == "quote-checked" else "fail"
    flag = "" if expect.startswith(got) else "  <== UNEXPECTED"
    print(f"{got:4} (expect {expect:22}) {name} {types}{flag}")
    if "-v" in sys.argv and types:
        for f in rep["failures"][:3]: print("        ", f["type"], f["where"], f["detail"][:160])
