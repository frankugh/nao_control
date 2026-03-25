from __future__ import annotations

import copy
import json
from pathlib import Path

from py3_script_runner.schema import validate_script


TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "script_builder_web" / "templates.json"


def _load_templates() -> dict:
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


def _append_snippet(script_obj: dict, snippet: object) -> None:
    steps = script_obj.setdefault("steps", [])
    assert isinstance(steps, list)
    if isinstance(snippet, dict):
        steps.append(copy.deepcopy(snippet))
        return
    assert isinstance(snippet, list)
    for item in snippet:
        assert isinstance(item, dict)
    steps.extend(copy.deepcopy(snippet))


def test_templates_file_has_expected_structure():
    data = _load_templates()
    assert isinstance(data, dict)
    assert isinstance(data.get("default_script"), dict)
    catalog = data.get("catalog")
    assert isinstance(catalog, list)
    assert catalog
    for category in catalog:
        assert isinstance(category, dict)
        assert isinstance(category.get("category_key"), str)
        assert category["category_key"].strip()
        assert isinstance(category.get("category_label"), str)
        assert category["category_label"].strip()
        templates = category.get("templates")
        assert isinstance(templates, list)
        assert templates
        for template in templates:
            assert isinstance(template, dict)
            assert isinstance(template.get("template_key"), str)
            assert template["template_key"].strip()
            assert isinstance(template.get("template_label"), str)
            assert template["template_label"].strip()
            assert "snippet" in template


def test_default_script_validates():
    data = _load_templates()
    validated = validate_script(copy.deepcopy(data["default_script"]))
    assert validated["version"] == 1
    assert "nao1" in validated["robots"]


def test_each_template_snippet_validates_after_append():
    data = _load_templates()
    default_script = data["default_script"]

    for category in data["catalog"]:
        for template in category["templates"]:
            script_obj = copy.deepcopy(default_script)
            _append_snippet(script_obj, template["snippet"])
            validated = validate_script(script_obj)
            assert len(validated["steps"]) >= 2


def test_summary_template_is_single_summary_start_step():
    data = _load_templates()
    summary_category = next(item for item in data["catalog"] if item["category_key"] == "summary")
    summary_template = next(item for item in summary_category["templates"] if item["template_key"] == "summary_start")
    snippet = summary_template["snippet"]
    assert isinstance(snippet, dict)

    script_obj = copy.deepcopy(data["default_script"])
    _append_snippet(script_obj, snippet)
    validated = validate_script(script_obj)

    modes = [step["action"].get("mode") for step in validated["steps"] if step["action"].get("type") == "do"]
    assert "summary_start" in modes
