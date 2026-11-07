"""UI text, the same contract as windows/App/Loc.cs: the English string is the key, so a missing translation falls back
to the original wording. The Chinese table is windows/App/Loc.cs's own, extracted into loc_zh.json (regenerate with
`python3 -m one401_gtk.loc --regenerate` from linux/; tests/test_linux_gui.py fails when the two drift apart).

Selection follows A1401_LANG, then LC_ALL / LC_MESSAGES / LANG. Keys keep the Windows "\\r\\n" line breaks so they
match Loc.cs exactly; text() turns them into plain newlines for GTK."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "loc_zh.json")


def _detect(environ=None):
    env = os.environ if environ is None else environ
    forced = env.get("A1401_LANG")
    if forced is not None:
        return "zh" if forced.lower().startswith("zh") else "en"
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name)
        if value:
            return "zh" if value.lower().startswith("zh") else "en"
    return "en"


LANGUAGE = _detect()
_ZH = None


def _table():
    global _ZH
    if _ZH is None:
        try:
            with open(TABLE, encoding="utf-8") as fh:
                _ZH = json.load(fh)
        except (OSError, ValueError):
            _ZH = {}
    return _ZH


def text(s):
    """Windows line breaks out, for display."""
    return s.replace("\r\n", "\n")


def T(en):
    """The English string, or its translation when the UI language is Chinese (Loc.T)."""
    if en is None or LANGUAGE != "zh":
        return text(en) if en is not None else en
    zh = _table().get(en)
    return text(zh if zh else en)


def F(en, *args):
    """Loc.F: a translated template with {0}-style holes filled in order."""
    t = T(en)
    for i, a in enumerate(args):
        t = t.replace("{" + str(i) + "}", str(a))
    return t


def Op(en):
    return T(en) if en in ("check", "build", "write") else en


def engine_language():
    """What the engine is told (A1401_LANG), as Engine.cs does."""
    return "zh-CN" if LANGUAGE == "zh" else "en"


# --- extraction from Loc.cs -------------------------------------------------------------------------------------------

_ESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "'": "'", "0": "\0"}


def _literals(src, pos):
    """C# string literal tokens joined by '+', starting at pos; returns (value, new pos)."""
    out = []
    while True:
        m = re.compile(r'\s*(//[^\n]*\n\s*)*"').match(src, pos)
        if not m:
            break
        i = m.end()
        buf = []
        while src[i] != '"':
            if src[i] == "\\":
                buf.append(_ESC.get(src[i + 1], src[i + 1]))
                i += 2
            else:
                buf.append(src[i])
                i += 1
        out.append("".join(buf))
        pos = i + 1
        plus = re.compile(r"\s*\+").match(src, pos)
        if not plus:
            break
        pos = plus.end()
    return "".join(out), pos


def parse_loc_cs(path):
    """The Zh dictionary of Loc.cs as {english: chinese}."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("new Dictionary<string, string>")
    start = src.index("{", start) + 1
    end = src.index("};", start)
    body = src[start:end]
    table = {}
    pos = 0
    entry = re.compile(r"\s*(?://[^\n]*\n\s*)*\{")
    while True:
        m = entry.search(body, pos)
        if not m:
            break
        key, pos = _literals(body, m.end())
        comma = re.compile(r"\s*,").match(body, pos)
        if not comma:
            raise ValueError("unexpected Loc.cs entry near " + body[m.end():m.end() + 60])
        value, pos = _literals(body, comma.end())
        close = re.compile(r"\s*\}").match(body, pos)
        if not close:
            raise ValueError("unexpected Loc.cs entry end near " + body[pos:pos + 60])
        pos = close.end()
        table[key] = value
    return table


if __name__ == "__main__":
    import sys
    if "--regenerate" in sys.argv:
        cs = os.path.join(HERE, "..", "..", "windows", "App", "Loc.cs")
        data = parse_loc_cs(cs)
        with open(TABLE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"wrote {len(data)} translations to {TABLE}")
