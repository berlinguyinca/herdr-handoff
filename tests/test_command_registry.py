import json
from pathlib import Path


def test_herdr_namespace_registry_contains_only_namespaced_commands():
    path = Path(__file__).parents[1] / "skill" / "herdr" / "commands.json"
    registry = json.loads(path.read_text())
    assert registry["syntax"] == "/herdr::<command>"
    assert set(registry["commands"]) == {
        "toClaude", "toPi", "toCodex", "toOpenCode", "artifact", "handoff",
        "takeover", "review", "verify", "fabric", "join", "publish", "observe", "session",
        "memory", "remember", "recall", "forget",
    }
    assert all(not name.startswith("/") for name in registry["commands"])


def test_memory_commands_delegate_to_the_installed_skill():
    path = Path(__file__).parents[1] / "skill" / "herdr" / "commands.json"
    registry = json.loads(path.read_text())
    for command in ("memory", "remember", "recall", "forget"):
        assert registry["commands"][command] == {
            "action": command,
            "skill": "herdr-memory",
        }


def test_existing_routes_and_handoff_aliases_are_preserved():
    path = Path(__file__).parents[1] / "skill" / "herdr" / "commands.json"
    registry = json.loads(path.read_text())
    assert registry["namespace"] == "herdr"
    commands = registry["commands"]
    for alias, harness in {
        "toClaude": "claude", "toPi": "pi", "toCodex": "codex",
        "toOpenCode": "opencode",
    }.items():
        assert commands[alias] == {
            "action": "handoff", "target_harness": harness,
        }
    for command in (
        "artifact", "handoff", "review", "verify", "fabric", "publish",
        "observe", "session",
    ):
        assert commands[command] == {"action": command}


def test_takeover_is_a_namespaced_route_of_its_own():
    # `to*` copies a session into a new pane; takeover replaces the pane, which is destructive, so it is
    # a distinct action and never an alias of `handoff`.
    registry = json.loads((Path(__file__).parents[1] / "skill" / "herdr" / "commands.json").read_text())
    assert registry["commands"]["takeover"] == {"action": "takeover"}
    assert registry["commands"]["handoff"] == {"action": "handoff"}


def test_join_delegates_to_the_installed_herdr_join_skill():
    path = Path(__file__).parents[1] / "skill" / "herdr" / "commands.json"
    registry = json.loads(path.read_text())
    assert registry["commands"]["join"] == {
        "action": "fabric-join",
        "skill": "herdr-join",
    }
