#!/usr/bin/env python3
"""Offline tests for radio-patch.py. Run: python3 -I radio-patch.test.py"""
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("radio_patch", ROOT / "radio-patch.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

FIXTURE = ROOT / "fixtures" / "page16.json"  # exported page, 13 buttons + nav keys


def load():
    return json.loads(FIXTURE.read_text())


class RadioPatchTests(unittest.TestCase):
    def test_apply_adds_only_the_radio_parts_to_listed_keys(self):
        d = load()
        out = rp.apply_radio(d, {"A": [("0", "1"), ("0", "2")]})
        for key in (("0", "1"), ("0", "2")):
            before = rp.get_button(d, key)
            after = rp.get_button(out, key)
            self.assertEqual([v["variableName"] for v in after["localVariables"]][-2:], ["radio_id", "radio_group"])
            self.assertEqual(len(after["localVariables"]), len(before["localVariables"]) + 2)
            down = after["steps"]["0"]["action_sets"]["down"]
            self.assertTrue(rp.is_radio_action(down[0]), "radio action must come first")
            self.assertEqual(len(down), len(before["steps"]["0"]["action_sets"]["down"]) + 1)
            self.assertTrue(rp.is_radio_feedback(after["feedbacks"][-1]), "radio feedback must come last")
            self.assertEqual(len(after["feedbacks"]), len(before["feedbacks"]) + 1)
            self.assertEqual(after["style"], before["style"], "button style must not change")
        # every other control is byte-for-byte untouched
        for r, row in d["page"]["controls"].items():
            for c, ctl in row.items():
                if (r, c) not in (("0", "1"), ("0", "2")):
                    self.assertEqual(out["page"]["controls"][r][c], ctl)

    def test_input_is_not_mutated(self):
        d = load()
        snapshot = copy.deepcopy(d)
        rp.apply_radio(d, {"A": [("0", "1")]})
        rp.strip_radio(d)
        self.assertEqual(d, snapshot)

    def test_strip_after_apply_restores_the_original_exactly(self):
        d = load()
        patched = rp.apply_radio(d, {"A": [("0", "1"), ("0", "2")], "B": [("2", "1"), ("2", "2")]})
        restored, n = rp.strip_radio(patched)
        self.assertEqual(n, 4)
        self.assertEqual(restored, d)

    def test_strip_only_listed_keys(self):
        d = load()
        patched = rp.apply_radio(d, {"A": [("0", "1"), ("0", "2")]})
        part, n = rp.strip_radio(patched, [("0", "1")])
        self.assertEqual(n, 1)
        self.assertEqual(rp.list_radio(part), {"A": [("0", "2")]})

    def test_list_reports_groups(self):
        d = load()
        patched = rp.apply_radio(d, {"A": [("0", "1")], "B": [("1", "1"), ("1", "2")]})
        self.assertEqual(rp.list_radio(patched), {"A": [("0", "1")], "B": [("1", "1"), ("1", "2")]})
        self.assertEqual(rp.list_radio(d), {})

    def test_every_step_gets_the_action(self):
        d = load()
        b = rp.get_button(d, ("0", "1"))
        b["steps"]["1"] = copy.deepcopy(b["steps"]["0"])
        out = rp.apply_radio(d, {"A": [("0", "1")]})
        for step in rp.get_button(out, ("0", "1"))["steps"].values():
            self.assertTrue(rp.is_radio_action(step["action_sets"]["down"][0]))

    def test_refuses_bad_input(self):
        d = load()
        with self.assertRaises(rp.PatchError):
            rp.apply_radio(d, {"A": [("0", "1")], "B": [("0", "1")]})  # key in two groups
        with self.assertRaises(rp.PatchError):
            rp.apply_radio(d, {"A": [("0", "0")]})  # navigation key, not a button
        with self.assertRaises(rp.PatchError):
            rp.apply_radio(d, {"A": [("3", "7")]})  # empty slot
        with self.assertRaises(rp.PatchError):
            rp.apply_radio(rp.apply_radio(d, {"A": [("0", "1")]}), {"B": [("0", "1")]})  # already patched
        for bad in ("A B=0:1", "A-1=0:1", "A=0-1", "A=", "=0:1", "A"):
            with self.assertRaises(rp.PatchError, msg=bad):
                rp.parse_group_spec(bad)
        with self.assertRaises(rp.PatchError):
            rp.parse_color("green")

    def test_refuses_button_without_a_background_layer(self):
        d = load()
        b = rp.get_button(d, ("0", "1"))
        b["style"]["layers"] = [l for l in b["style"]["layers"] if l["type"] != "box"]
        with self.assertRaises(rp.PatchError):
            rp.apply_radio(d, {"A": [("0", "1")]})

    def test_new_entity_ids_are_unique(self):
        d = load()
        out = rp.apply_radio(d, {"A": [("0", "1"), ("0", "2"), ("0", "3")]})
        ids = []

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("id"), str) and len(o["id"]) == 21:
                    ids.append(o["id"])
                if "overrideId" in o:
                    ids.append(o["overrideId"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(out)
        self.assertEqual(len(ids), len(set(ids)))

    def test_verify_ignores_ids_only(self):
        d = load()
        a = rp.apply_radio(d, {"A": [("0", "1")]})
        b = copy.deepcopy(a)
        b["page"]["controls"]["0"]["1"]["feedbacks"][-1]["id"] = "x" * 21
        self.assertTrue(rp.same_ignoring_ids(a, b))
        b["page"]["controls"]["0"]["1"]["feedbacks"][-1]["options"]["expression"]["value"] += " "
        self.assertFalse(rp.same_ignoring_ids(a, b))

    def test_sync_adds_changes_removes_and_leaves_the_rest(self):
        d = load()
        start, s = rp.sync_radio(d, {("0", "1"): "A", ("0", "2"): "A", ("2", "1"): "B"})
        self.assertEqual(s, {"added": 3, "changed": 0, "removed": 0, "unchanged": 0})
        self.assertEqual(rp.list_radio(start), {"A": [("0", "1"), ("0", "2")], "B": [("2", "1")]})
        # move 0:2 to group B, drop 2:1, add 1:1 to A, keep 0:1
        out, s = rp.sync_radio(start, {("0", "1"): "A", ("0", "2"): "B", ("1", "1"): "A"})
        self.assertEqual(s, {"added": 1, "changed": 1, "removed": 1, "unchanged": 1})
        self.assertEqual(rp.list_radio(out), {"A": [("0", "1"), ("1", "1")], "B": [("0", "2")]})
        # the unchanged button is byte-identical (not stripped and re-added)
        self.assertEqual(out["page"]["controls"]["0"]["1"], start["page"]["controls"]["0"]["1"])
        # nothing but the radio parts ever changes
        self.assertTrue(rp.same_ignoring_ids(rp.strip_radio(out)[0], d))

    def test_sync_with_no_change_is_a_no_op_and_empty_removes_everything(self):
        d = load()
        start, _ = rp.sync_radio(d, {("0", "1"): "A"})
        same, s = rp.sync_radio(start, {("0", "1"): "A"})
        self.assertEqual(same, start)
        self.assertEqual(s, {"added": 0, "changed": 0, "removed": 0, "unchanged": 1})
        gone, s = rp.sync_radio(start, {})
        self.assertEqual(s["removed"], 1)
        self.assertEqual(gone, d)

    def test_sync_rejects_bad_labels_and_keys(self):
        d = load()
        with self.assertRaises(rp.PatchError):
            rp.sync_radio(d, {("0", "1"): "A B"})
        with self.assertRaises(rp.PatchError):
            rp.sync_radio(d, {("0", "0"): "A"})  # navigation key

    # ---- looks: on (selected) and off (not selected) backgrounds and text, plus an on border

    @staticmethod
    def overrides(button, inverted):
        fbs = [f for f in button["feedbacks"] if rp.is_radio_feedback(f) and bool(f["isInverted"]["value"]) == inverted]
        return {(o["elementId"], o["elementProperty"]): o["override"]["value"] for f in fbs for o in f["styleOverrides"]}

    def test_default_is_a_gold_border_and_no_off_feedback(self):
        out = rp.apply_radio(load(), {"A": [("0", "1")]})
        b = rp.get_button(out, ("0", "1"))
        self.assertEqual(rp.style_of(b), rp.DEFAULT_STYLE)
        self.assertEqual(self.overrides(b, False), {("box0", "borderWidth"): rp.BORDER_WIDTH, ("box0", "borderColor"): 0xFFCC00})
        self.assertEqual(len([f for f in b["feedbacks"] if rp.is_radio_feedback(f)]), 1)

    def test_on_and_off_backgrounds_and_text(self):
        style = rp.Style(on_bg=0x00AA00, on_text=0xFFFFFF, on_border=None, off_bg=0x202020, off_text=0xAAAAAA)
        b = rp.get_button(rp.apply_radio(load(), {"A": [("0", "1")]}, style), ("0", "1"))
        self.assertEqual(self.overrides(b, False), {("box0", "color"): 0x00AA00, ("text0", "color"): 0xFFFFFF})
        self.assertEqual(self.overrides(b, True), {("box0", "color"): 0x202020, ("text0", "color"): 0xAAAAAA})
        on, off = [f for f in b["feedbacks"] if rp.is_radio_feedback(f)]
        self.assertFalse(on["isInverted"]["value"])
        self.assertTrue(off["isInverted"]["value"], "the off look is the inverse of the on condition")
        self.assertEqual(on["options"], off["options"], "same condition, opposite sense")
        self.assertEqual(rp.style_of(b), style)

    def test_border_combines_with_colours(self):
        style = rp.Style(on_bg=0x003300, on_text=None, on_border=0xFFCC00, off_bg=None, off_text=None)
        b = rp.get_button(rp.apply_radio(load(), {"A": [("0", "1")]}, style), ("0", "1"))
        self.assertEqual(self.overrides(b, False), {("box0", "color"): 0x003300, ("box0", "borderWidth"): rp.BORDER_WIDTH, ("box0", "borderColor"): 0xFFCC00})
        self.assertEqual(rp.style_of(b), style)

    def test_off_state_is_never_given_a_border(self):
        style = rp.Style(on_bg=0x00AA00, on_text=None, on_border=0xFFCC00, off_bg=0x202020, off_text=None)
        b = rp.get_button(rp.apply_radio(load(), {"A": [("0", "1")]}, style), ("0", "1"))
        self.assertNotIn(("box0", "borderColor"), self.overrides(b, True))

    def test_auto_text_is_readable_on_each_states_background(self):
        style = rp.Style(on_bg=0x003300, on_text="auto", on_border=None, off_bg=0xEBEBEB, off_text="auto")
        b = rp.get_button(rp.apply_radio(load(), {"A": [("0", "1")]}, style), ("0", "1"))
        self.assertEqual(self.overrides(b, False)[("text0", "color")], 0xFFFFFF)  # white on dark green
        self.assertEqual(self.overrides(b, True)[("text0", "color")], 0x000000)   # black on light grey
        # auto with no background to read against means "keep the button's own text"
        style = rp.Style(on_bg=None, on_text="auto", on_border=0xFFCC00, off_bg=None, off_text="auto")
        b = rp.get_button(rp.apply_radio(load(), {"A": [("0", "1")]}, style), ("0", "1"))
        self.assertNotIn(("text0", "color"), self.overrides(b, False))
        self.assertEqual(len([f for f in b["feedbacks"] if rp.is_radio_feedback(f)]), 1, "nothing to do in the off state")

    def test_contrast_color(self):
        self.assertEqual(rp.contrast_color(0xFFFFFF), 0x000000)
        self.assertEqual(rp.contrast_color(0x000000), 0xFFFFFF)
        self.assertEqual(rp.contrast_color(0xFFCC00), 0x000000)
        self.assertEqual(rp.contrast_color(0x003300), 0xFFFFFF)
        self.assertEqual(rp.contrast_color(0x660000), 0xFFFFFF)

    def test_the_buttons_own_look_is_never_modified(self):
        d = load()
        b = rp.get_button(d, ("0", "1"))
        box = next(l for l in b["style"]["layers"] if l["type"] == "box")
        txt = next(l for l in b["style"]["layers"] if l["type"] == "text")
        box["color"] = {"isExpression": False, "value": 0xEBEBEB}
        txt["color"] = {"isExpression": False, "value": 0x000000}
        style = rp.Style(0x00AA00, "auto", 0xFFCC00, 0x202020, "auto")
        out = rp.apply_radio(d, {"A": [("0", "1")]}, style)
        self.assertEqual(rp.get_button(out, ("0", "1"))["style"], b["style"])
        restored, _ = rp.strip_radio(out)
        self.assertEqual(restored, d, "stripping brings back exactly the original, off feedback included")

    def test_strip_removes_both_feedbacks(self):
        style = rp.Style(0x00AA00, None, None, 0x202020, None)
        out = rp.apply_radio(load(), {"A": [("0", "1")]}, style)
        self.assertEqual(len([f for f in rp.get_button(out, ("0", "1"))["feedbacks"] if rp.is_radio_feedback(f)]), 2)
        self.assertEqual(rp.strip_radio(out)[0], load())

    def test_style_of_reads_legacy_buttons(self):
        # first version: background override only; second: border / fill with contrast text, single feedback
        out = rp.apply_radio(load(), {"A": [("0", "1")]}, rp.Style(13056, None, None, None, None))
        self.assertEqual(rp.style_of(rp.get_button(out, ("0", "1"))), rp.Style(13056, None, None, None, None))
        self.assertIsNone(rp.style_of(rp.get_button(load(), ("0", "1"))))

    def test_parse_style_and_validation(self):
        s = rp.parse_style({"on": {"bg": "#00aa00", "text": "auto", "border": None}, "off": {"bg": "#202020", "text": "#ffffff"}})
        self.assertEqual(s, rp.Style(0x00AA00, "auto", None, 0x202020, 0xFFFFFF))
        self.assertEqual(rp.parse_style(rp.style_to_json(s)), s)
        for bad in (
            "border", None,
            {"on": {}, "off": {"bg": "#202020"}},                         # nothing changes when selected
            {"on": {"bg": "red"}},                                         # bad colour
            {"on": {"bg": "auto"}},                                        # auto only for text
            {"on": {"bg": "#00aa00"}, "off": {"bg": -1}},
            {"on": {"text": "auto"}},                                      # auto with no background: nothing to change
            {"on": "x"},
        ):
            with self.assertRaises(rp.PatchError, msg=bad):
                rp.parse_style(bad)

    def test_sync_restyles_only_when_the_look_differs(self):
        d = load()
        gold = rp.DEFAULT_STYLE
        start, _ = rp.sync_radio(d, {("0", "1"): ("A", gold)})
        same, s = rp.sync_radio(start, {("0", "1"): ("A", gold)})
        self.assertEqual((same, s["unchanged"], s["changed"]), (start, 1, 0))
        kept, s = rp.sync_radio(start, {("0", "1"): "A"})  # no style given: keep what is there
        self.assertEqual((kept, s["unchanged"]), (start, 1))
        green = rp.Style(0x003300, "auto", None, 0x202020, "auto")
        re, s = rp.sync_radio(start, {("0", "1"): ("A", green)})
        self.assertEqual(s["changed"], 1)
        self.assertEqual(rp.style_of(rp.get_button(re, ("0", "1"))), rp.resolve_style(green))
        self.assertTrue(rp.same_ignoring_ids(rp.strip_radio(re)[0], d))
        # asking for the same look again, with auto unresolved, is not a change
        again, s = rp.sync_radio(re, {("0", "1"): ("A", green)})
        self.assertEqual((again, s["unchanged"]), (re, 1))
        # dropping the off colours is a change, and removes the off feedback
        no_off, s = rp.sync_radio(re, {("0", "1"): ("A", rp.Style(0x003300, "auto", None, None, None))})
        self.assertEqual(s["changed"], 1)
        self.assertEqual(len([f for f in rp.get_button(no_off, ("0", "1"))["feedbacks"] if rp.is_radio_feedback(f)]), 1)

    def test_sync_keeps_the_current_look_when_only_the_group_changes(self):
        d = load()
        look = rp.Style(0x003300, "auto", None, 0x202020, "auto")
        start, _ = rp.sync_radio(d, {("0", "1"): ("A", look)})
        moved, s = rp.sync_radio(start, {("0", "1"): "B"})
        self.assertEqual(s["changed"], 1)
        self.assertEqual(rp.style_of(rp.get_button(moved, ("0", "1"))), rp.resolve_style(look))
        self.assertEqual(rp.list_radio(moved), {"B": [("0", "1")]})

    def test_sync_new_buttons_without_a_look_get_the_default(self):
        mine = rp.Style(0x112233, None, None, None, None)
        out, _ = rp.sync_radio(load(), {("0", "1"): "A"}, mine)
        self.assertEqual(rp.style_of(rp.get_button(out, ("0", "1"))), mine)
        out, _ = rp.sync_radio(load(), {("0", "1"): "A"})
        self.assertEqual(rp.style_of(rp.get_button(out, ("0", "1"))), rp.DEFAULT_STYLE)

    def test_sync_rejects_a_bad_look(self):
        with self.assertRaises(rp.PatchError):
            rp.sync_radio(load(), {("0", "1"): ("A", rp.Style(None, None, None, 0x202020, None))})

    def test_cli_colour_options(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "styled"
            code = rp.main(["apply", str(FIXTURE), str(out), "-g", "A=0:1", "--on-bg", "#00aa00", "--on-text", "auto", "--off-bg", "#202020"])
            self.assertEqual(code, 0)
            b = rp.get_button(rp.load_page(str(out) + ".json"), ("0", "1"))
            self.assertEqual(rp.style_of(b), rp.Style(0x00AA00, 0xFFFFFF, None, 0x202020, None))
            self.assertEqual(rp.main(["apply", str(FIXTURE), str(out), "-g", "A=0:1", "--off-bg", "#202020"]), 2)  # on look unchanged

    def test_cli_round_trip_through_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "patched"
            self.assertEqual(rp.main(["apply", str(FIXTURE), str(out), "-g", "A=0:1,0:2", "-g", "B=2:1"]), 0)
            self.assertEqual(rp.main(["list", str(out) + ".companionconfig"]), 0)  # gzip input
            back = Path(tmp) / "back"
            self.assertEqual(rp.main(["strip", str(out) + ".json", str(back)]), 0)
            self.assertEqual(rp.main(["verify", str(FIXTURE), str(back) + ".json"]), 0)
            self.assertEqual(rp.main(["verify", str(FIXTURE), str(out) + ".json"]), 1)
            self.assertEqual(rp.main(["apply", str(FIXTURE), str(out), "-g", "A=0:0"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
