import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_engine_pin_updates_are_documented_as_manual() -> None:
    workflow = (ROOT / ".github/workflows/customer-impact.yml").read_text(
        encoding="utf-8"
    )
    engine_checkout = workflow.split("repository: KanopyAI/kanopy-release-engine", 1)[1]
    assert re.search(r"ref: [0-9a-f]{40} # v\d+\.\d+\.\d+", engine_checkout)

    guide = (ROOT / "docs/release-communication.md").read_text(encoding="utf-8")
    instructions = " ".join(
        guide.split("Change the engine with a PR there, then", 1)[1]
        .split("For local commands", 1)[0]
        .split()
    )
    assert "by hand" in instructions, "The checkout ref needs a manual update"
    assert "`ref`" in instructions
    assert "version comment" in instructions
    assert "stamped internal impact entry" in instructions
    assert "Dependabot" not in instructions
