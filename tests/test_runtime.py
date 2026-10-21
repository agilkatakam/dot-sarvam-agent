import pytest

from agent import runtime


def test_defaults_load_and_save_roundtrip():
    values = runtime.load()
    assert values == runtime.defaults()
    saved = runtime.save({"tts_voice": "priya", "llm_temperature": "0.2", "max_call_seconds": 120.0})
    assert saved["tts_voice"] == "priya" and saved["llm_temperature"] == 0.2 and saved["max_call_seconds"] == 120
    assert runtime.load()["tts_voice"] == "priya"


@pytest.mark.parametrize("changes", [{"tts_voice": "darth"}, {"llm_temperature": 9}, {"nope": 1}, {"max_call_seconds": "abc"}])
def test_bad_values_are_rejected(changes):
    with pytest.raises(ValueError):
        runtime.save(changes)


def test_schema_covers_every_field():
    assert {f["key"] for f in runtime.schema()} == set(runtime.defaults())
