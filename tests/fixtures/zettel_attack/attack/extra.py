# ruff: noqa
# Copy of the round-1 code reviewer's attack script (fixture; kept as written).
import sys, shutil, re
from pathlib import Path
sys.argv=[sys.argv[0]]
exec(open(Path(__file__).parent/'run.py').read().split("out = R / \"attack/notes\"")[0])
cases.clear()
for v in ("9yFK1MTYVFE","yXYXqeTrXRU"): shutil.copy(R/f"stg_all/lit/vid-{v}.md", LIT)
# Round 4: the contract examples cite fictional transcripts (package B fixtures).
for f in (R/"stg_fictional"/"lit").glob("*.md"): shutil.copy(f, LIT)
# contract 11.1 verbatim
c = (R/"repo/zettel_ralph/NOTE_CONTRACT.md").read_text()
ex1 = re.search(r"### 14.1.*?```markdown\n(.*?)```", c, re.S).group(1)
case("C1 NOTE_CONTRACT 14.1 example verbatim", ex1, "pass")
ex2 = re.search(r"### 14.2.*?```markdown\n(.*?)```", c, re.S).group(1)
case("C2 NOTE_CONTRACT 14.2 example verbatim", ex2, "pass")
ST = '"i train rings twice per week ... 30 to 40 seconds of volume per session"'
case("K11b stitched quote, 1225 chars apart, as extra Evidence item", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B, ev=EV_K.replace("\n## Connected", f"- vid-krqBQjUydGY (SasaVenos): {ST}\n\n## Connected")), "fail")
case("K11c stitched quote supports a merged claim 'twice per week ... 30 to 40 seconds per session'", note_k(GOOD_TITLE, GOOD_LEAD, [f"He trains rings twice per week, 30 to 40 seconds of volume per session. {T}"], ev=EV_K.replace(f'"{Q1}"', ST)), "fail")
case("K33 number hidden in [brackets] in a Details bullet", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around 10 sets of holds per week [20 for advanced athletes]. {T}"]), "fail")
case("K34 degraded transcript cited (9yFK1MTYVFE)", note_k("Muscle Strength Matters", f"You have to have a very high level of muscle strength. [src: vid-9yFK1MTYVFE]", [f"Strength matters. [src: vid-9yFK1MTYVFE]"], fm=FM_K.replace("krqBQjUydGY","9yFK1MTYVFE").replace('"SasaVenos"','"sthenics_"'), ev='\n## Evidence\n\n- vid-9yFK1MTYVFE (sthenics_): "You have to have a very high level of muscle strength."\n\n## Connected Ideas\n\n- [[Other]] — x\n'), "fail")
case("K35 invented speaker on title-mention transcript (yXYX, 'Raphael Paz')", note_k("Height Matters", "Height is an important factor. [src: vid-yXYXqeTrXRU]", ["Height is one of the important factors. [src: vid-yXYXqeTrXRU]"], fm=FM_K.replace("krqBQjUydGY","yXYXqeTrXRU").replace('speaker: "SasaVenos"','speaker: "Raphael Paz"').replace('channel: "SasaVenos"','channel: "sthenics_"'), ev='\n## Evidence\n\n- vid-yXYXqeTrXRU (Raphael Paz): "important factors if you look at it broadly enough"\n\n## Connected Ideas\n\n- [[Other]] — x\n'), "fail")
case("K36 '10 sets of holds' -> '10 holds' per week", note_k(GOOD_TITLE, GOOD_LEAD, [f"He does around 10 holds per week. {T}"]), "fail")
case("K37 '80 to 90 percent' -> '80-90%' (form)", note_k(GOOD_TITLE, GOOD_LEAD, GOOD_B+[f"He trains at 80-90% of his max. {T}"], ev=EV_K.replace("\n## Connected", '- vid-krqBQjUydGY (SasaVenos): "only perform movements and isometrics that are in my 80 to 90 percent rep max"\n\n## Connected')), "pass")
case("R6b quote crossing marker, title w/o numbers", note_r(title="Radoslav Radev Limits Failure Workouts", lead=f"Radoslav Radev limits failure workouts. {TR}", ev=EV_R(q="go to a failure. And my advice for you is if you train four times per week only one maximum two to go to a failure.")), "pass")
case("R9b bracket correction plunge [planche]", note_r(title="Radoslav Radev Says Overtraining Loses Progress", ev=EV_R(q="Probably a lot of you, you was better before on plunge [planche] and", ts="02:26"), bullets=[f"Many viewers were better at planche before. [src: vid-KCRETdZ0l78 @ 02:26]"], lead=f"Radoslav Radev says overtraining loses progress. [src: vid-KCRETdZ0l78 @ 02:26]"), "pass")
case("R14 'Disagreement' quote fabricated, sources only KCRET", note_r(extra='\n## Disagreement\n\n- SasaVenos [src: vid-BjZmElNOI0g]: "always train to failure 6 times per week"\n'), "fail")
case("R15 tag @01:50 but Evidence for it at @05:00 elsewhere", note_r(), "pass")
out = R / "attack/notes2"
if out.exists(): shutil.rmtree(out)
out.mkdir()
lit = vc.LitIndex([LIT])
for i,(name,text,expect) in enumerate(cases):
    h1=re.search(r"^# (.+)$", text, re.M).group(1); (out/f"{i:02d}").mkdir(); p=out/f"{i:02d}"/f"{h1}.md"; p.write_text(text)
    rep=vc.verify_note(p, lit)
    types=sorted({f["type"] for f in rep["failures"]})
    got="pass" if rep["status"]=="quote-checked" else "fail"
    print(f"{got:4} (expect {expect:8}) {name} {types} warn={sorted({w['type'] for w in rep['warnings']})}{'' if expect.startswith(got) else '  <== UNEXPECTED'}")
    for f in rep["failures"][:2]: print("        ", f["type"], f["where"], f["detail"][:150])
