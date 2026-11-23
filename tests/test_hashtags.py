"""Hashtags made without an AI: three to five fitting ones, added to the description unless the person wrote their own.
Runs against the code directly; nothing leaves this machine."""
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-tags-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import db, hashtags as h, posting  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


TITLE = "Kai Cenat's First Time Playing GTA 6!"
SAID = "Yo they locked me up bro. Actual prison, actual Prison man. I'm not going back to the prison. Fuck that shit, the cop said stick up"

tags = h.make(SAID, TITLE, "Kai Cenat Live", "GTA 6", "funny", ["GTA6", "gaming", "x"])
check("three to five tags", 3 <= len(tags) <= 5, str(tags))
check("the game comes first", tags[0] == "gta6", str(tags))
check("the channel is a tag", "kaicenat" in tags, str(tags))
check("a word said again and again is a tag", "prison" in tags, str(tags))
check("no rude words and no filler words", not set(tags) & {"fuck", "shit", "actual", "they", "that"}, str(tags))
check("no tag repeats", len(set(tags)) == len(tags))
check("the AI's own sensible tags are used, its junk is not", "gaming" in tags and "x" not in tags, str(tags))
check("a name with a number in the title is a tag", "gta6" in h.make("", TITLE, "", None), str(h.make("", TITLE, "", None)))
check("with nothing to go on there are still three", len(h.make()) == 3, str(h.make()))
check("a clip's kind can be a tag", "fails" in h.make("hello there", "Some video", "Some Channel", None, "fail"))
check("every tag is plain letters and digits", all(t.isalnum() and t == t.lower() for t in tags), str(tags))
check("a channel name loses its Live and Official", (h.channel_tag("Kai Cenat Live"), h.channel_tag("Pewdiepie Official")) == ("kaicenat", "pewdiepie"))
check("at most five even with many to choose from", len(h.make(SAID, TITLE, "Kai Cenat", "GTA 6", "funny", ["a1b", "c2d", "e3f", "g4h", "i5j", "k6l"])) == 5)

NL = chr(10)
words = [{"w": w, "s": 10 + i, "e": 10.5 + i} for i, w in enumerate(SAID.split())]
clip = {"start": 10, "end": 40, "category": "fail", "description": "Kai gets stuck up #gta", "hashtags": ["GTA6"]}
h.apply(clip, words, TITLE, "Kai Cenat Live", "GTA 6")
check("the tags end the description", clip["description"].startswith("Kai gets stuck up" + NL * 2 + "#gta6 ") and clip["description"].count("#") == len(clip["hashtags"]), clip["description"])
check("a tag the AI wrote into its sentence is not said twice", clip["description"].count("#gta ") == 0 and "#gta" not in clip["description"].split(NL * 2)[0])
check("the clip is marked, so posting does not add them again", clip["hashtags_in_description"] is True and posting._caption(clip, {"post_footer": ""}) == clip["description"])

own = {"start": 10, "end": 40, "description": "My own text #mine", "fixed_description": True, "hashtags": ["old"]}
h.apply(own, words, TITLE, "Kai Cenat Live", "GTA 6")
check("a description the person wrote is left exactly as it is", own["description"] == "My own text #mine" and own["hashtags"] == ["old"] and "hashtags_in_description" not in own)

empty = {"start": 10, "end": 40, "description": "", "hashtags": []}
h.apply(empty, words, TITLE, "Kai Cenat Live", "GTA 6")
check("with no description at all, the tags are the description", empty["description"].startswith("#") and NL not in empty["description"], empty["description"])
check("a clip made before this keeps posting as it did", posting._caption({"description": "D", "hashtags": ["a", "b"]}, {"post_footer": "F"}) == "D" + NL * 2 + "#a #b" + NL * 2 + "F")

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil  # noqa: E402

db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
