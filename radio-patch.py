#!/usr/bin/env python3
"""Add / remove / inspect radio-group behaviour on buttons of an exported page.

Pure offline tool: it reads a Companion single-page export and writes a new
export. It never talks to Companion. Import the result with `radio load` on
the computer that runs Companion.

  radio-patch.py list   <page>
  radio-patch.py apply  <page> <out-base> -g A=0:1,0:2 [-g B=1:1,1:2] [--on-bg C] [--on-text C|auto] [--on-border C] [--off-bg C] [--off-text C|auto]
  radio-patch.py strip  <page> <out-base> [--keys 0:1,0:2]
  radio-patch.py verify <expected> <actual>

<page> is a page export (gzip as served by /int/export/page/N, or plain JSON).
Keys are "row:column" as in the export (both start at 0). <out-base> gets
.json and .companionconfig written next to it.

What `apply` adds to each listed button (nothing else is touched):
  - local variable radio_group  (the group label, free text)
  - local variable radio_id     (page+row+column, unique per key)
  - first "down" action of every step: set custom variable radio_<group> to
    radio_id (variable is created on the first press)
  - last feedback(s): the "on" look while radio_<group> == radio_id and, if you
    set any, the "off" look while it is not

Each state can set a background colour and a text colour (the "on" state can also
draw a border). Anything left unset keeps the button's own look. For text, "auto"
means black or white, whichever reads best on that state's background. With no
colour options at all, "on" is a gold border and "off" is the button's own look.

The parts are recognised by content (not entity id: Companion regenerates
ids on import), so `strip` removes exactly what `apply` added.
"""
import argparse
import copy
import gzip
import json
import re
import secrets
import string
import sys
from collections import namedtuple
from pathlib import Path

ALPHABET = string.ascii_letters + string.digits + "_-"
GROUP_RE = re.compile(r"^[A-Za-z0-9_]+$")
KEY_RE = re.compile(r"^(\d+):(\d+)$")

LOCAL_GROUP = "radio_group"
LOCAL_ID = "radio_id"
ACTION_NAME_EXPR = "concat('radio_', $(local:radio_group))"
ACTION_VALUE = "$(local:radio_id)"
FEEDBACK_EXPR = (
    "concat(getVariable(concat('custom:radio_', $(local:radio_group)))) == concat($(local:radio_id))"
)
ID_STARTUP = "$(this:page)$(this:row)$(this:column)"

BORDER_WIDTH = 6
DEFAULT_COLOR = 0xFFCC00  # gold: visible on light and dark buttons

# A look for both states. Every field is None (keep the button's own), an int colour, or,
# for the two text fields, "auto" (black/white, whichever reads best on that state's background).
Style = namedtuple("Style", "on_bg on_text on_border off_bg off_text")
DEFAULT_STYLE = Style(None, None, DEFAULT_COLOR, None, None)


class PatchError(Exception):
    pass


def new_id():
    return "".join(secrets.choice(ALPHABET) for _ in range(21))


def lit(value):
    return {"isExpression": False, "value": value}


def expr(value):
    return {"isExpression": True, "value": value}


# ---------------------------------------------------------------- io

def load_page(path):
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    d = json.loads(raw)
    if d.get("type") != "page":
        raise PatchError(f"{path}: not a single-page export (type={d.get('type')!r})")
    return d


def write_page(d, base):
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps(d, indent=1))
    with gzip.open(base.with_suffix(".companionconfig"), "wt", encoding="utf-8") as f:
        f.write(json.dumps(d))


# ---------------------------------------------------------------- recognisers

def is_radio_action(a):
    if a.get("definitionId") != "custom_variable_set_value":
        return False
    name = a.get("options", {}).get("name", {})
    return bool(name.get("isExpression")) and str(name.get("value", "")).startswith("concat('radio_'")


def is_radio_feedback(f):
    if f.get("definitionId") != "check_expression":
        return False
    e = f.get("options", {}).get("expression", {})
    return str(e.get("value", "")).startswith("concat(getVariable(concat('custom:radio_'")


def is_radio_local(v):
    return v.get("variableName") in (LOCAL_GROUP, LOCAL_ID)


def radio_group_of(button):
    for v in button.get("localVariables", []):
        if v.get("variableName") == LOCAL_GROUP:
            return v["options"]["startup_value"]["value"]
    return None


def has_radio(button):
    return (
        any(is_radio_local(v) for v in button.get("localVariables", []))
        or any(is_radio_feedback(f) for f in button.get("feedbacks", []))
        or any(
            is_radio_action(a)
            for step in button.get("steps", {}).values()
            for acts in step.get("action_sets", {}).values()
            if isinstance(acts, list)
            for a in acts
        )
    )


def style_of(button):
    """The Style found in a button's radio feedbacks, or None if it has no radio feedback.

    Reads the "on" feedback (not inverted) and the "off" feedback (inverted); works
    for buttons made by any earlier version. Colours are always concrete ints (never "auto").
    """
    fbs = [f for f in button.get("feedbacks", []) if is_radio_feedback(f)]
    if not fbs:
        return None
    layers = button["style"]["layers"]
    box_ids = {l["id"] for l in layers if l.get("type") == "box"}
    text_ids = {l["id"] for l in layers if l.get("type") == "text"}
    vals = {"on_bg": None, "on_text": None, "on_border": None, "off_bg": None, "off_text": None}
    for f in fbs:
        state = "off_" if f.get("isInverted", {}).get("value") else "on_"
        for o in f.get("styleOverrides", []):
            prop, element, value = o.get("elementProperty"), o.get("elementId"), o.get("override", {}).get("value")
            if prop == "color" and element in box_ids:
                vals[state + "bg"] = value
            elif prop == "color" and element in text_ids:
                vals[state + "text"] = value
            elif prop == "borderColor" and element in box_ids and state == "on_":
                vals["on_border"] = value
    return Style(**vals)


# ---------------------------------------------------------------- helpers

def parse_keys(text):
    keys = []
    for part in text.split(","):
        part = part.strip()
        m = KEY_RE.match(part)
        if not m:
            raise PatchError(f"bad key {part!r}: expected row:column, e.g. 0:1")
        keys.append((m.group(1), m.group(2)))
    return keys


def parse_group_spec(spec):
    if "=" not in spec:
        raise PatchError(f"bad group spec {spec!r}: expected LABEL=row:col,row:col")
    label, keys = spec.split("=", 1)
    if not GROUP_RE.match(label):
        raise PatchError(f"bad group label {label!r}: use letters, digits or underscore only")
    return label, parse_keys(keys)


def parse_color(value):
    if isinstance(value, bool):
        raise PatchError(f"bad color {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise PatchError(f"bad color {value!r}: out of range")
        return value
    text = str(value).strip()
    if text.startswith("#"):
        if not re.match(r"^#[0-9a-fA-F]{6}$", text):
            raise PatchError(f"bad color {text!r}: expected #rrggbb")
        return int(text[1:], 16)
    if not text.isdigit():
        raise PatchError(f"bad color {text!r}: expected #rrggbb or a non-negative integer")
    return parse_color(int(text))


def contrast_color(color):
    """Black or white, whichever reads better on `color`."""
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    return 0x000000 if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else 0xFFFFFF


def color_hex(color):
    return "#%06x" % color


def resolve_style(style):
    """Replace "auto" text colours by black/white readable on that state's background.

    "auto" with no background colour set for the state means "keep" (None).
    """
    def text(value, bg):
        if value == "auto":
            return contrast_color(bg) if bg is not None else None
        return value

    return Style(style.on_bg, text(style.on_text, style.on_bg), style.on_border,
                 style.off_bg, text(style.off_text, style.off_bg))


def check_style(style):
    """Validate a (possibly unresolved) Style; returns it unchanged."""
    for name in ("on_bg", "on_border", "off_bg"):
        v = getattr(style, name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFFFF):
            raise PatchError(f"bad colour for {name}: {v!r}")
    for name in ("on_text", "off_text"):
        v = getattr(style, name)
        if v is not None and v != "auto" and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFFFF):
            raise PatchError(f"bad colour for {name}: {v!r}")
    r = resolve_style(style)
    if r.on_bg is None and r.on_text is None and r.on_border is None:
        raise PatchError("the selected (on) look must change at least one of background, text or border")
    return style


def parse_style(obj):
    """{"on": {"bg", "text", "border"}, "off": {"bg", "text"}} -> Style.

    Each value is null (keep the button's own), "#rrggbb" / an int, or "auto" (text only).
    """
    if not isinstance(obj, dict):
        raise PatchError("style must be an object")
    on, off = obj.get("on") or {}, obj.get("off") or {}
    if not isinstance(on, dict) or not isinstance(off, dict):
        raise PatchError("style.on and style.off must be objects")

    def col(value, allow_auto=False):
        if value is None:
            return None
        if value == "auto":
            if not allow_auto:
                raise PatchError("'auto' is only valid for text colours")
            return "auto"
        return parse_color(value)

    return check_style(Style(col(on.get("bg")), col(on.get("text"), True), col(on.get("border")),
                             col(off.get("bg")), col(off.get("text"), True)))


def style_to_json(style):
    """Style -> the JSON shape parse_style accepts (colours as #rrggbb)."""
    def c(v):
        return v if v is None or v == "auto" else color_hex(v)

    return {"on": {"bg": c(style.on_bg), "text": c(style.on_text), "border": c(style.on_border)},
            "off": {"bg": c(style.off_bg), "text": c(style.off_text)}}


def get_button(d, key, must_exist=True):
    r, c = key
    ctl = d["page"]["controls"].get(r, {}).get(c)
    if ctl is None:
        if must_exist:
            raise PatchError(f"no control at row {r}, column {c}")
        return None
    if ctl.get("type") != "button-layered":
        raise PatchError(f"row {r}, column {c} is a {ctl.get('type')!r}, not a regular button")
    return ctl


def background_layer_id(button):
    boxes = [l for l in button["style"]["layers"] if l.get("type") == "box"]
    if not boxes:
        raise PatchError("button has no box (background) layer to colour")
    named = [l for l in boxes if l.get("name") == "Background"]
    return (named or boxes)[0]["id"]


def text_layer_id(button):
    texts = [l for l in button["style"]["layers"] if l.get("type") == "text"]
    return texts[0]["id"] if texts else None


# ---------------------------------------------------------------- operations

def local_user_value(name, startup):
    return {
        "id": new_id(),
        "definitionId": "user_value",
        "connectionId": "internal",
        "options": {"startup_value": lit(startup), "persist_value": lit(False)},
        "type": "feedback",
        "style": {},
        "isInverted": lit(False),
        "variableName": name,
        "children": {},
    }


def radio_action():
    return {
        "id": new_id(),
        "definitionId": "custom_variable_set_value",
        "connectionId": "internal",
        "options": {"name": expr(ACTION_NAME_EXPR), "create": lit(True), "value": lit(ACTION_VALUE)},
        "type": "action",
        "children": {},
    }


def radio_feedbacks(button, style):
    """The radio feedback(s) for `style`: always an "on" one, plus an inverted "off" one if needed."""
    s = resolve_style(check_style(style))
    box, text = background_layer_id(button), text_layer_id(button)
    if text is None and (s.on_text is not None or s.off_text is not None):
        raise PatchError("a text colour was requested but the button has no text layer")

    def feedback(inverted, props):
        overrides = [
            {"overrideId": new_id(), "elementId": element, "elementProperty": prop, "override": lit(value)}
            for element, prop, value in props
        ]
        return {
            "id": new_id(),
            "definitionId": "check_expression",
            "connectionId": "internal",
            "options": {"expression": expr(FEEDBACK_EXPR)},
            "type": "feedback",
            "isInverted": lit(inverted),
            "styleOverrides": overrides,
            "children": {},
        }

    on, off = [], []
    if s.on_bg is not None:
        on.append((box, "color", s.on_bg))
    if s.on_text is not None:
        on.append((text, "color", s.on_text))
    if s.on_border is not None:
        on += [(box, "borderWidth", BORDER_WIDTH), (box, "borderColor", s.on_border)]
    if s.off_bg is not None:
        off.append((box, "color", s.off_bg))
    if s.off_text is not None:
        off.append((text, "color", s.off_text))
    return [feedback(False, on)] + ([feedback(True, off)] if off else [])


def _apply_one(b, key, label, style):
    if has_radio(b):
        raise PatchError(f"key {key[0]}:{key[1]} already has radio parts; strip it first")
    clash = [v["variableName"] for v in b.get("localVariables", []) if v.get("variableName") in (LOCAL_GROUP, LOCAL_ID)]
    if clash:
        raise PatchError(f"key {key[0]}:{key[1]} already has local variable(s) {clash}")
    fbs = radio_feedbacks(b, style)  # raises first if the button cannot show the style
    b.setdefault("localVariables", []).extend(
        [local_user_value(LOCAL_ID, ID_STARTUP), local_user_value(LOCAL_GROUP, label)]
    )
    steps = b.setdefault("steps", {})
    if not steps:
        steps["0"] = {"action_sets": {"down": [], "up": []}, "options": {"runWhileHeld": []}}
    for step in steps.values():
        sets = step.setdefault("action_sets", {})
        sets["down"] = [radio_action()] + list(sets.get("down", []))
    b.setdefault("feedbacks", []).extend(fbs)


def apply_radio(d, groups, style=DEFAULT_STYLE):
    """groups: {label: [(row, col), ...]}; every key gets `style`. Returns a patched deep copy."""
    out = copy.deepcopy(d)
    seen = {}
    for label, keys in groups.items():
        for key in keys:
            if key in seen:
                raise PatchError(f"key {key[0]}:{key[1]} is in both group {seen[key]} and {label}")
            seen[key] = label
    for key, label in seen.items():
        _apply_one(get_button(out, key), key, label, style)
    return out


def strip_radio(d, keys=None):
    """Remove radio parts from the listed keys (all keys if None). Returns (copy, count)."""
    out = copy.deepcopy(d)
    count = 0
    wanted = set(keys) if keys is not None else None
    for r, row in out["page"]["controls"].items():
        for c, b in row.items():
            if b.get("type") != "button-layered" or (wanted is not None and (r, c) not in wanted):
                continue
            if not has_radio(b):
                continue
            b["localVariables"] = [v for v in b.get("localVariables", []) if not is_radio_local(v)]
            b["feedbacks"] = [f for f in b.get("feedbacks", []) if not is_radio_feedback(f)]
            for step in b.get("steps", {}).values():
                for name, acts in step.get("action_sets", {}).items():
                    if isinstance(acts, list):
                        step["action_sets"][name] = [a for a in acts if not is_radio_action(a)]
            count += 1
    return out, count


def list_radio(d):
    """{label: [(row, col), ...]} for every button carrying radio parts."""
    found = {}
    for r, row in d["page"]["controls"].items():
        for c, b in row.items():
            if b.get("type") == "button-layered" and has_radio(b):
                found.setdefault(radio_group_of(b) or "(incomplete)", []).append((r, c))
    return found


def sync_radio(d, desired, default_style=DEFAULT_STYLE):
    """Make the page's radio buttons match `desired`.

    desired: {(row, col): label} or {(row, col): (label, Style_or_None)}.
    A style of None means "keep the button's current look" (new buttons get `default_style`).
    "auto" text colours are resolved first, so a button already showing the requested
    look counts as unchanged. A button is left alone when its group and look already
    match; otherwise it is stripped and re-applied.
    Returns (new page, {"added", "changed", "removed", "unchanged"}).
    """
    wanted = {}
    for key, value in desired.items():
        label, style = (value, None) if isinstance(value, str) else value
        if not GROUP_RE.match(str(label)):
            raise PatchError(f"bad group label {label!r}: use letters, digits or underscore only")
        if style is not None:
            style = resolve_style(check_style(style))
        get_button(d, key)  # must be a regular button
        wanted[key] = (label, style)

    current = {}
    for r, row in d["page"]["controls"].items():
        for c, b in row.items():
            if b.get("type") == "button-layered" and has_radio(b):
                current[(r, c)] = (radio_group_of(b) or "(incomplete)", style_of(b))

    default_style = resolve_style(check_style(default_style))

    def effective_style(key):
        style = wanted[key][1]
        if style is not None:
            return style
        old = current.get(key, (None, None))[1]
        if old is not None and (old.on_bg is not None or old.on_text is not None or old.on_border is not None):
            return old
        return default_style

    def same(key):
        if key not in current:
            return False
        label, style = wanted[key]
        return current[key][0] == label and (style is None or current[key][1] == style)

    to_strip = [k for k in current if k not in wanted or not same(k)]
    out = strip_radio(d, to_strip)[0] if to_strip else copy.deepcopy(d)
    for key in wanted:
        if not same(key):
            _apply_one(get_button(out, key), key, wanted[key][0], effective_style(key))
    summary = {
        "added": sum(1 for k in wanted if k not in current),
        "changed": sum(1 for k in wanted if k in current and not same(k)),
        "removed": sum(1 for k in current if k not in wanted),
        "unchanged": sum(1 for k in wanted if same(k)),
    }
    return out, summary


def _without_ids(o):
    if isinstance(o, dict):
        return {k: _without_ids(v) for k, v in o.items() if k not in ("id", "overrideId")}
    if isinstance(o, list):
        return [_without_ids(x) for x in o]
    return o


def same_ignoring_ids(a, b):
    return _without_ids(a["page"]) == _without_ids(b["page"])


# ---------------------------------------------------------------- cli

def style_from_args(a):
    """Style from the --on-*/--off-* options; no options at all means DEFAULT_STYLE."""
    given = {k: getattr(a, k) for k in ("on_bg", "on_text", "on_border", "off_bg", "off_text")}
    if all(v is None for v in given.values()):
        return DEFAULT_STYLE
    return parse_style({"on": {"bg": given["on_bg"], "text": given["on_text"], "border": given["on_border"]},
                        "off": {"bg": given["off_bg"], "text": given["off_text"]}})

def _fmt(groups):
    return "; ".join(f"{g}: " + " ".join(f"{r}:{c}" for r, c in keys) for g, keys in sorted(groups.items())) or "(none)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("page")
    p = sub.add_parser("apply"); p.add_argument("page"); p.add_argument("out")
    p.add_argument("-g", "--group", action="append", required=True, metavar="LABEL=row:col,...")
    p.add_argument("--on-bg", help="background when selected, #rrggbb")
    p.add_argument("--on-text", help="text colour when selected, #rrggbb or auto")
    p.add_argument("--on-border", help="border colour when selected, #rrggbb")
    p.add_argument("--off-bg", help="background when not selected, #rrggbb")
    p.add_argument("--off-text", help="text colour when not selected, #rrggbb or auto")
    p = sub.add_parser("strip"); p.add_argument("page"); p.add_argument("out"); p.add_argument("--keys")
    p = sub.add_parser("verify"); p.add_argument("expected"); p.add_argument("actual")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "list":
            d = load_page(a.page)
            print(f"page {d['page']['name']!r}: radio groups -> {_fmt(list_radio(d))}")
        elif a.cmd == "apply":
            d = load_page(a.page)
            groups = dict(parse_group_spec(s) for s in a.group)
            out = apply_radio(d, groups, style_from_args(a))
            write_page(out, a.out)
            print(f"patched {sum(len(k) for k in groups.values())} buttons -> {_fmt(list_radio(out))}")
            print(f"wrote {a.out}.json and {a.out}.companionconfig")
        elif a.cmd == "strip":
            d = load_page(a.page)
            out, n = strip_radio(d, parse_keys(a.keys) if a.keys else None)
            write_page(out, a.out)
            print(f"stripped radio parts from {n} buttons; remaining groups -> {_fmt(list_radio(out))}")
            print(f"wrote {a.out}.json and {a.out}.companionconfig")
        elif a.cmd == "verify":
            ok = same_ignoring_ids(load_page(a.expected), load_page(a.actual))
            print("IDENTICAL (ignoring entity ids)" if ok else "DIFFERENT")
            return 0 if ok else 1
    except PatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
