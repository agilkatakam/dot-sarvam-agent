import copy

import pytest

from agent import prompt


def doc():
    return copy.deepcopy(prompt.default_doc())


def values():
    return prompt.variables(doc(), "CALENDAR", "Tuesday 14 October 2026")


def test_default_prompt_is_valid_and_renders_everything():
    d = doc()
    assert prompt.validate(d) == []
    text = prompt.render(d, values())
    for s in d["sections"]:
        if s["enabled"] and s["title"]:
            assert s["title"].upper() in text
    assert "CALENDAR" in text and "Tuesday 14 October 2026" in text
    assert "{" not in text.replace("{}", "")  # every variable filled


def test_disabled_sections_are_left_out():
    d = doc()
    target = next(s for s in d["sections"] if s["id"] == "objections")
    target["enabled"] = False
    assert target["title"].upper() not in prompt.render(d, values())


def test_unknown_braces_are_left_alone():
    assert prompt._fill("{org} says {not_a_var}", {"org": "Dot"}) == "Dot says {not_a_var}"


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(opening_line=""), "empty"),
    (lambda d: d["sections"].append(dict(d["sections"][0])), "duplicate"),
    (lambda d: [s for s in d["sections"] if s["id"] == "ending"][0].update(enabled=False), "ending"),
    (lambda d: [s for s in d["sections"] if s["kind"] == "personas"][0]["personas"][0].update(pitch=""), "pitch"),
    (lambda d: [s for s in d["sections"] if s["kind"] == "personas"][0]["personas"][0].update(key="unclear"), "already used"),
])
def test_validation_catches_unsafe_edits(mutate, message):
    d = doc()
    mutate(d)
    errors = prompt.validate(d)
    assert errors and any(message in e for e in errors), errors


def test_versions_and_activation():
    v1, d1 = prompt.active()
    assert v1 == 1 and prompt.active()[0] == 1  # seeded once
    d = doc()
    personas = next(s for s in d["sections"] if s["kind"] == "personas")
    personas["personas"].append({"key": "it_head", "name": "IT head", "pitch": "Runs on any browser.", "questions": []})
    v2 = prompt.save(d, "add IT head")
    assert prompt.active()[0] == v2
    assert "it_head" in prompt.persona_tags(prompt.active()[1])
    prompt.activate(v1)
    assert prompt.active()[0] == v1
    with pytest.raises(ValueError):
        prompt.save({"opening_line": "", "sections": []}, "broken")
    with pytest.raises(KeyError):
        prompt.activate(999)


def test_render_parts_matches_sections():
    d = doc()
    parts = prompt.render_parts(d, values())
    assert [p["id"] for p in parts] == [s["id"] for s in d["sections"]]
