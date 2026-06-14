"""Phase 5: volume monitor (parsers, baseline, anomaly, cooldown, watchlist) + commands."""

import common


def test_vol_config_defaults_present():
    assert common.VOL_SPIKE_MULT == 2.5
    assert common.VOL_TRAILING_N == 20
    assert common.VOL_MIN_SAMPLES == 5
    assert common.VOL_ALERT_COOLDOWN_HRS == 12.0
    assert common.VOL_FLOOR_EQUITY == 0.0
    assert common.VOL_FLOOR_CRYPTO == 0.0
    assert common.VOL_FLOOR_PREDICTION == 0.0
