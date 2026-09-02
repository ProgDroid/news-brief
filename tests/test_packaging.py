"""The Dockerfile's COPY allowlist is the only place in this repo that says
"these files and no others". Nothing else can catch a module missing from it:
pytest and CI both run against a full checkout, where every module is present
by definition, so the import that fails in the image succeeds everywhere it is
tested. The failure surfaces as a container that dies on startup -- and
docker-compose sets `restart: "no"`, so it dies quietly rather than
crash-looping.

That is how config.py reached production absent from the image (news-brief-kpc):
it was added by the config-to-rows work, imported at module level by both
supervisor.py and brief.py, and listed in neither the COPY line nor the
workflow's paths filter.

These tests read the import graph rather than a hand-maintained list, so the
next module added is covered without anyone remembering to come back here.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"


def _top_level_module_files() -> set[str]:
    """Every module that lives at the repo root, as a bare filename."""
    return {p.name for p in REPO_ROOT.glob("*.py")}


def _copy_listed_modules() -> set[str]:
    """The .py filenames named on the Dockerfile's flat COPY lines.

    Only lines copying into the image root (`COPY a.py b.py .`) are read;
    directory copies like `COPY enrichment/ ./enrichment/` bring their own
    contents and are not an allowlist of anything.
    """
    listed: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or not stripped.endswith(" ."):
            continue
        listed |= {tok for tok in stripped.split()[1:-1] if tok.endswith(".py")}
    return listed


def _imported_modules(path: Path, candidates: set[str]) -> set[str]:
    """Repo-root modules that `path` imports, at any nesting depth.

    A function-level import of an absent module crashes just as hard as a
    module-level one, only later and on a rarer path, so both count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import inside a package, not a root module.
            names = [node.module] if node.module and node.level == 0 else []
        else:
            continue
        for name in names:
            root = f"{name.split('.')[0]}.py"
            if root in candidates:
                found.add(root)
    return found


def _required_modules() -> set[str]:
    """The transitive closure of what the copied modules actually import.

    Seeded from the COPY list rather than from a named entry point: every
    module in the image is reachable by some job mode, and a module is required
    if anything already shipping needs it.
    """
    candidates = _top_level_module_files()
    seen: set[str] = set()
    queue = list(_copy_listed_modules() & candidates)
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(_imported_modules(REPO_ROOT / current, candidates) - seen)
    return seen


def test_every_module_the_image_imports_is_copied_into_it():
    missing = sorted(_required_modules() - _copy_listed_modules())
    assert not missing, (
        f"{missing} are imported by modules already in the image but are not "
        f"in the Dockerfile COPY list. The container will die on import with "
        f'ModuleNotFoundError, and `restart: "no"` means it dies silently.'
    )


def _workflow_path_triggers() -> set[str]:
    """The `paths:` entries of the build workflow's push trigger."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n    paths:\n((?:      - .*\n)+)", text)
    assert block, "could not find the paths: filter in the build workflow"
    return set(re.findall(r"- '([^']+)'", block.group(1)))


def _workflow_ruff_file_lists() -> list[set[str]]:
    """The explicit file arguments of each ruff invocation in the workflow.

    These are enumerated rather than run as `ruff check .`, so a module absent
    from them is silently never linted -- the run stays green by not looking.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    lists = [
        {tok for tok in line.split() if tok.endswith(".py")}
        for line in text.splitlines()
        if line.strip().startswith("ruff ")
    ]
    assert lists, "could not find a ruff invocation in the build workflow"
    return lists


def test_every_copied_module_is_linted_in_ci():
    """A module in the image but absent from ruff's argument list is not
    checked by anything: the local gate runs `ruff check .` over the whole
    tree, so the divergence shows up only in CI, as an absence."""
    for files in _workflow_ruff_file_lists():
        unlinted = sorted(_copy_listed_modules() - files)
        assert not unlinted, (
            f"{unlinted} ship in the image but are missing from a ruff "
            f"invocation in the build workflow, so CI never lints them."
        )


def test_every_copied_module_triggers_a_rebuild_when_it_changes():
    """A module in the image but absent from `paths:` is worse than untested:
    editing it alone publishes no new image, so the fix appears committed while
    production keeps running the old code."""
    untriggered = sorted(_copy_listed_modules() - _workflow_path_triggers())
    assert not untriggered, (
        f"{untriggered} ship in the image but are not in the build workflow's "
        f"paths: filter, so changing one of them alone publishes nothing."
    )
