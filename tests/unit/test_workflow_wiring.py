"""The graph names prompts, models and tools by id only; the content is assembled in `workflow/__init__.py` and resolved by factories."""

import json
from pathlib import Path

from retail_kyc_onboarding.workflow import WORKFLOW

GRAPH_PY = Path(__file__).resolve().parents[2] / "retail_kyc_onboarding" / "workflow" / "graph.py"


def test_graph_py_imports_no_component():
    source = GRAPH_PY.read_text(encoding="utf-8")
    assert "components" not in source and "prompts." not in source and "models." not in source


def ids(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and isinstance(v, str):
                yield v
            yield from ids(v, key)
    elif isinstance(value, list):
        for v in value:
            yield from ids(v, key)


def test_every_id_the_graph_names_is_an_entity_of_the_workflow():
    graph = json.loads(WORKFLOW.graph.model_dump_json())
    assert set(ids(graph, "prompt")) == set(WORKFLOW.prompts)
    assert set(ids(graph, "model")) <= set(WORKFLOW.models)
    tools = {t for node in graph["nodes"] for t in node["config"].get("tools", [])}
    assert tools == set(WORKFLOW.tools)
