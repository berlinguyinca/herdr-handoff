import json
from pathlib import Path


def test_herdr_namespace_registry_contains_only_namespaced_commands():
    path = Path(__file__).parents[1] / "skill" / "herdr" / "commands.json"
    registry = json.loads(path.read_text())
    assert registry["syntax"] == "/herdr::<command>"
    assert set(registry["commands"]) == {
        "toClaude", "toPi", "toCodex", "toOpenCode", "artifact", "handoff",
        "review", "verify", "fabric", "publish", "observe", "session",
    }
    assert all(not name.startswith("/") for name in registry["commands"])
