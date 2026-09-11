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
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

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


def _copy_listed_packages() -> set[Path]:
    """The directories the Dockerfile copies wholesale into the image.

    The counterpart to `_copy_listed_modules`: those lines end in a bare `.`
    and name files, these name a directory and bring their whole contents. Read
    from the Dockerfile rather than listed here so a new shipped package is
    covered without an edit -- the same reason every other derivation in this
    file reads the real artifact.
    """
    found: set[Path] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or stripped.endswith(" ."):
            continue
        src = stripped.split()[1]
        if src.endswith("/"):
            found.add(REPO_ROOT / src.rstrip("/"))
    return found


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


# ── The compose anchor ────────────────────────────────────────────────────────
# The same shape as the COPY allowlist above, one layer out. Compose passes
# through only what the anchor names, so a missing line is a knob that silently
# keeps its default -- the bug the settings table was built to retire. What the
# table cannot retire is the opposite error: a line that IS there, crosses the
# container boundary correctly, and names a variable no code ever looks up. It
# reads as configured from every angle. That is how NEWSBRIEF_CAPTURE_ENABLED
# sat in the anchor while `common.KNOBS` called the knob CAPTURE_ENABLED
# (news-brief-bz1), and why the operator's flip did nothing.
#
# test_config.py cannot catch this: it exercises the importer with fabricated
# knobs and never reads the real compose file.
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _anchor_variables() -> set[str]:
    """Every variable the `x-newsbrief` anchor passes into the container."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("x-newsbrief:"))
    after = lines[start + 1 :]
    end = next(
        (i for i, line in enumerate(after) if line and not line[0].isspace()),
        len(after),
    )
    found = {
        m.group(1)
        for line in after[:end]
        if (m := re.match(r"\s*-\s*([A-Z0-9_]+)=", line))
    }
    assert found, "could not find any environment lines in the x-newsbrief anchor"
    return found


def _consumed_variables() -> set[str]:
    """Every variable name something in the image actually reads.

    Derived from three places rather than listed, so a new secret or knob needs
    no edit here: the settings knobs by their stored key, the direct os.environ
    reads, and db's discrete connection variables, which are held in a table and
    so are invisible to a scan for os.environ literals.

    The os.environ scan covers the shipped PACKAGES as well as the top-level
    modules. It read only `REPO_ROOT.glob("*.py")` until news-brief-qx4, which
    made the blind spot load-bearing: BIGDATA_API_KEY is a credential and so is
    read from the environment rather than held as a settings row, and that read
    lives in enrichment/config.py. A top-level-only scan cannot see it, so
    declaring the variable in the anchor -- which is required, or enrichment
    runs dark -- made the test below report it as read by nothing. An absence
    test whose evidence-gathering cannot reach part of the image reports a
    clean negative for the part it cannot see.
    """
    import common
    import db

    reads = re.compile(
        r"os\.(?:environ(?:\.get)?|getenv)\s*[\(\[]\s*[\"']([A-Z0-9_]+)[\"']"
    )
    sources = list(REPO_ROOT.glob("*.py"))
    for package in _copy_listed_packages():
        sources.extend(package.rglob("*.py"))
    direct: set[str] = set()
    for path in sources:
        direct |= set(reads.findall(path.read_text(encoding="utf-8")))
    return (
        {spec.key(name) for name, spec in common.KNOBS.items()}
        | direct
        | {name for name, _ in db._DISCRETE}
        | {"POSTGRES_PORT"}  # read with a default, via a literal db.py catches
    )


def test_the_anchor_parser_sees_a_variable_that_is_read():
    """The presence control for the test below. Its assertion is an absence, and
    an absence is satisfied for free by a parser that returns nothing -- so this
    pins a variable that must appear on BOTH sides, and would fail if either
    derivation silently stopped finding anything."""
    assert "BRIEF_MEMORY_ENABLED" in _anchor_variables()
    assert "BRIEF_MEMORY_ENABLED" in _consumed_variables()
    assert "ANTHROPIC_API_KEY" in _consumed_variables()


def test_the_consumed_scan_reaches_into_shipped_packages():
    """The second presence control, for the half of the image that is not a
    top-level module.

    BRIEF_MEMORY_ENABLED above proves the scan finds SOMETHING, which a top-level-only
    glob satisfies. This pins a variable read exclusively from a shipped
    package -- BIGDATA_API_KEY, read once in enrichment/config.py and held in
    no settings row because it is a credential. Narrow the glob back to
    `REPO_ROOT.glob("*.py")` and this fails while BRIEF_MEMORY_ENABLED still passes,
    which is the distinction the earlier control could not draw.
    """
    assert _copy_listed_packages(), (
        "no directory COPY lines found in the Dockerfile; the package scan "
        "below is then silently equivalent to a top-level-only scan"
    )
    assert "BIGDATA_API_KEY" in _anchor_variables(), (
        "the credential must cross the container boundary, or providers.py "
        "falls back to the null provider and enrichment runs dark"
    )
    assert "BIGDATA_API_KEY" in _consumed_variables(), (
        "enrichment/config.py reads BIGDATA_API_KEY from the environment; a "
        "scan that misses it reports a read variable as read by nothing"
    )


def test_every_variable_the_anchor_passes_through_is_read_by_something():
    """A variable declared here and read nowhere is configuration theatre: the
    operator sets it, recreates the container, and nothing happens -- with no
    error, because a value nobody looks up cannot fail to convert."""
    unread = sorted(_anchor_variables() - _consumed_variables())
    assert not unread, (
        f"{unread} are passed into the container by the x-newsbrief anchor but "
        f"are read by nothing -- not a common.KNOBS key, not an os.environ read, "
        f"not a db connection variable. Setting one of them has no effect."
    )


# ── Scripts must be runnable the way the Dockerfile documents ────────────────
#
# Same class as the COPY allowlist above, and it bit on 2026-09-08: a script
# with 14 passing tests died on the host with `ModuleNotFoundError: No module
# named 'db'`. Nothing in the suite could catch it, because pytest imports a
# script as `scripts.<name>` with the repo root already on sys.path, while
# running one as a PATH puts `scripts/` at sys.path[0] and the repo root
# nowhere. The two older scripts carry a path shim for exactly this reason and
# neither had a test holding it there -- so the convention was load-bearing,
# undocumented outside a comment, and free to be forgotten by the next file.


def _runnable_scripts() -> list[Path]:
    return sorted(
        p for p in (REPO_ROOT / "scripts").glob("*.py") if p.stem != "__init__"
    )


@pytest.mark.parametrize("script", _runnable_scripts(), ids=lambda p: p.stem)
def test_a_script_run_as_a_path_can_import_the_root_modules(script):
    """Run it the way the Dockerfile's comment says to, and watch it resolve.

    DATABASE_URL is stripped so nothing connects: every one of these exits early
    or dies on the missing connection, and either is fine. The assertion is
    narrow on purpose -- this is about import resolution, not about the script
    doing its job, and widening it would make the test fail for reasons that
    have nothing to do with what it guards.
    """
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}

    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert "ModuleNotFoundError" not in done.stderr, (
        f"{script.name} cannot import a root module when run as a path. "
        f"It needs the sys.path shim its siblings carry.\n{done.stderr[-600:]}"
    )
