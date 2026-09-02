"""The enrichment package's configuration seam.

`enrichment/config.py` used to own its own `os.environ.get` constants. From
0q0.7.6 the non-secret ones are `settings` rows in `common.KNOBS`, and this
module forwards to them so the ~10 `config.ENRICHMENT_X` call sites did not have
to change. What must be true is that the forwarding is LIVE: the whole point of
moving the knobs is that a host toggle lands without recreating a container.
"""

import pytest

import common
import enrichment
from enrichment import config


def test_the_forwarded_knobs_resolve_through_common(monkeypatch):
    assert config.ENRICHMENT_ENABLED is False
    monkeypatch.setattr(common, "ENRICHMENT_ENABLED", True)
    assert config.ENRICHMENT_ENABLED is True


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ENRICHMENT_ENABLED", False),
        ("ENRICHMENT_PROVIDER", ""),
        ("ENRICHMENT_THEMES_ENABLED", True),
        ("ENRICHMENT_MAX_SYMBOLS", 20),
        ("ENRICHMENT_MAX_THEMES", 8),
        ("ENRICHMENT_HTTP_TIMEOUT", 20.0),
        ("BIGDATA_BASE_URL", "https://api.bigdata.com"),
    ],
)
def test_every_forwarded_knob_keeps_its_old_default(name, expected):
    assert getattr(config, name) == expected


def test_the_secret_and_the_fixture_dir_stay_in_the_environment():
    """`BIGDATA_API_KEY` is a credential — a settings row would put it in every
    `pg_dump`. `ENRICHMENT_FIXTURE_DIR` is test-only and is set by tests that
    have no database. Both stay real module constants read from the
    environment."""
    assert "BIGDATA_API_KEY" in vars(config)
    assert "FIXTURE_DIR" in vars(config)


def test_an_unknown_name_raises_rather_than_forwarding():
    """The forwarding list is explicit, so `config.PG_A_ENABLED` is a typo and
    not a lookup that happens to succeed through `common`."""
    with pytest.raises(AttributeError, match="PG_A_ENABLED"):
        config.PG_A_ENABLED


def test_is_enabled_is_not_frozen_at_import(monkeypatch):
    """`enrichment/__init__.py` bound the flag with `from .config import
    ENRICHMENT_ENABLED` — a from-import copy, frozen at import, which no host
    toggle could ever move. Moving the knob to a row is worthless while that
    copy exists."""
    assert enrichment.is_enabled() is False
    monkeypatch.setattr(common, "ENRICHMENT_ENABLED", True)
    assert enrichment.is_enabled() is True


def test_the_environment_no_longer_enables_enrichment(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_ENABLED", "1")
    assert config.ENRICHMENT_ENABLED is False
