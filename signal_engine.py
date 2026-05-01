"""Signal engine. PriceState -> Signal(direction, confidence, price)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from price_feed import PriceState

# Signal weights (must sum to 1.0)
W_DELTA = 0.40
W_MOMENTUM = 0.25
W_VOLUME = 0.20
W_VWAP = 0.15

# Delta |pct| -> base confidence curve, as (abs_delta_ratio, confidence)
# delta_from_open is a ratio (e.g. 0.0005 = 0.05%)
DELTA_CURVE = [
    (0.00005, 0.10),  # <0.005%  noise
    (0.0001,  0.20),  # 0.01%
    (0.0002,  0.35),  # 0.02%
    (0.0005,  0.50),  # 0.05%
    (0.0010,  0.65),  # 0.10%
    (0.0020,  0.75),  # 0.20%
    (0.0050,  0.85),  # 0.50%
    (0.0100,  0.92),  # 1.00%
]

CONFIDENCE_CAP = 0.95

# Normalization scales for alignment signals
MOMENTUM_SCALE = 5.0        # $/tick that counts as "strong" momentum
VWAP_DIV_SCALE = 0.0005     # 0.05% separation between price and vwap = strong

# Time-boost coefficients (param_sweep mutates these)
BOOST_30 = 1.08             # T-30s confidence multiplier
BOOST_15 = 1.15             # T-15s confidence multiplier


@dataclass
class Signal:
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0 - 1.0
    suggested_price: float  # 0.88 - 0.95
    rationale: str
    timestamp: float
    window_delta: float
    seconds_to_close: float
    expected_value: float


def _interp(x: float, curve: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation. x>=0. Extends flat beyond endpoints."""
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for i in range(1, len(curve)):
        x0, y0 = curve[i - 1]
        x1, y1 = curve[i]
        if x <= x1:
            t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + t * (y1 - y0)
    return curve[-1][1]


def delta_confidence(abs_delta: float) -> float:
    return _interp(abs_delta, DELTA_CURVE)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def confidence_to_price(confidence: float) -> float:
    """Map confidence -> maker limit price. Higher conf => higher price (thinner margin).

    Anchors (from spec):
      0.55 -> 0.88
      0.65 -> 0.90
      0.75 -> 0.92
      0.85 -> 0.93
      0.95 -> 0.95
    """
    curve = [
        (0.55, 0.88),
        (0.65, 0.90),
        (0.75, 0.92),
        (0.85, 0.93),
        (0.95, 0.95),
    ]
    price = _interp(confidence, curve)
    price = min(price, 0.95)  # Hard cap per spec
    return round(price, 2)


def expected_value(confidence: float, entry_price: float) -> float:
    """EV per share: (p * profit) - ((1-p) * cost).

    profit per share if win = (1.0 - entry_price)
    cost per share if lose = entry_price
    """
    profit = 1.0 - entry_price
    cost = entry_price
    return confidence * profit - (1 - confidence) * cost


def compute_signal(
    state: PriceState,
    window_end_ts: float,
    now: Optional[float] = None,
) -> Optional[Signal]:
    """Compute a Signal from current PriceState. Return None if data insufficient."""
    if state.current_price <= 0 or state.window_open_price <= 0:
        return None

    t = now if now is not None else time.time()
    seconds_to_close = max(0.0, window_end_ts - t)

    delta = state.delta_from_open
    abs_delta = state.delta_from_open_abs
    delta_dir = 1 if delta > 0 else (-1 if delta < 0 else 0)

    base_delta_conf = delta_confidence(abs_delta)

    # Momentum alignment: positive momentum in same direction as delta confirms.
    # Normalize momentum to -1..+1 via tanh-like clamp
    mom_norm = _clamp(state.momentum / MOMENTUM_SCALE, -1.0, 1.0)
    # alignment sign: +1 confirms delta, -1 contradicts
    if delta_dir != 0:
        momentum_alignment = mom_norm * delta_dir
    else:
        momentum_alignment = mom_norm  # use raw if delta is flat

    # Volume imbalance in -1..+1 already. Align with delta.
    if delta_dir != 0:
        volume_alignment = _clamp(state.volume_imbalance, -1.0, 1.0) * delta_dir
    else:
        volume_alignment = _clamp(state.volume_imbalance, -1.0, 1.0)

    # VWAP position: price above vwap = bullish
    if state.vwap > 0:
        vwap_div = (state.current_price - state.vwap) / state.vwap
    else:
        vwap_div = 0.0
    vwap_norm = _clamp(vwap_div / VWAP_DIV_SCALE, -1.0, 1.0)
    if delta_dir != 0:
        vwap_alignment = vwap_norm * delta_dir
    else:
        vwap_alignment = vwap_norm

    # Composite. Delta signed, others are alignments.
    if delta_dir == 0:
        # No delta direction — fall back to volume + momentum + vwap raw signed sum
        composite = (W_MOMENTUM * mom_norm
                     + W_VOLUME * _clamp(state.volume_imbalance, -1.0, 1.0)
                     + W_VWAP * vwap_norm)
    else:
        composite = (
            W_DELTA * base_delta_conf * delta_dir
            + W_MOMENTUM * momentum_alignment * delta_dir
            + W_VOLUME * volume_alignment * delta_dir
            + W_VWAP * vwap_alignment * delta_dir
        )

    direction = "UP" if composite >= 0 else "DOWN"
    confidence = abs(composite)

    # Time boost — closer to close = stronger
    if seconds_to_close <= 15:
        confidence *= BOOST_15
    elif seconds_to_close <= 30:
        confidence *= BOOST_30

    confidence = _clamp(confidence, 0.0, CONFIDENCE_CAP)
    confidence = _apply_variant(confidence, state)
    suggested_price = confidence_to_price(confidence)
    ev = expected_value(confidence, suggested_price)

    rationale = (
        f"dir={direction} conf={confidence:.2f} "
        f"delta={delta*100:+.4f}% (base={base_delta_conf:.2f}) "
        f"mom={state.momentum:+.3f} (aln={momentum_alignment:+.2f}) "
        f"vol_imb={state.volume_imbalance:+.2f} (aln={volume_alignment:+.2f}) "
        f"vwap_div={vwap_div*100:+.4f}% (aln={vwap_alignment:+.2f}) "
        f"T-{seconds_to_close:.0f}s"
    )

    return Signal(
        direction=direction,
        confidence=confidence,
        suggested_price=suggested_price,
        rationale=rationale,
        timestamp=t,
        window_delta=delta,
        seconds_to_close=seconds_to_close,
        expected_value=ev,
    )


def compute_contrarian_signal(
    state: PriceState,
    ask_up: float,
    ask_down: float,
) -> Optional[Signal]:
    """Contrarian fade: when one side's ask is at/above CONTRARIAN_ASK_THRESHOLD,
    bet the underdog at its current ask. Returns None if neither side qualifies
    or either ask is missing.

    Distinct from `compute_signal` — does not predict direction from PriceState
    features, only reacts to extreme mispricing on the order book.
    """
    threshold = CONFIG.contrarian_ask_threshold
    if ask_up <= 0 or ask_down <= 0:
        return None
    if max(ask_up, ask_down) < threshold:
        return None

    if ask_up >= ask_down:
        favourite_dir = "UP"
        favourite_ask = ask_up
        underdog_dir = "DOWN"
        underdog_ask = ask_down
    else:
        favourite_dir = "DOWN"
        favourite_ask = ask_down
        underdog_dir = "UP"
        underdog_ask = ask_up

    suggested_price = max(0.02, round(underdog_ask, 2))
    confidence = max(0.0, 1.0 - favourite_ask)

    return Signal(
        direction=underdog_dir,
        confidence=confidence,
        suggested_price=suggested_price,
        rationale=(
            f"contrarian: fav={favourite_dir}@{favourite_ask:.2f} "
            f"-> bet underdog {underdog_dir} at {suggested_price:.2f}"
        ),
        timestamp=time.time(),
        window_delta=state.delta_from_open,
        seconds_to_close=0.0,
        expected_value=0.0,
    )


def should_trade(signal: Signal) -> bool:
    if signal.confidence < CONFIG.min_confidence:
        return False
    if signal.expected_value <= 0:
        return False
    if signal.seconds_to_close < 5:
        return False
    return True


def gate_vs_market(signal: Signal, ask_up: float, ask_down: float) -> bool:
    if signal.seconds_to_close < 5:
        return False
    if signal.confidence < CONFIG.min_confidence:
        return False
    ask = ask_up if signal.direction == "UP" else ask_down
    if ask <= 0:
        return False
    edge_threshold = _edge_threshold(signal.direction)
    if signal.confidence - ask < edge_threshold:
        return False
    if abs(signal.window_delta) < CONFIG.min_delta_pct:
        return False
    return True


# --- variant helpers ---

_REMAP_CACHE: Optional[list[tuple[float, float, float]]] = None
_REMAP_PATH_LOADED: Optional[str] = None


def _load_remap() -> list[tuple[float, float, float]]:
    """Load + cache the calibration remap. Returns list of (raw_lo, raw_hi, calibrated)."""
    global _REMAP_CACHE, _REMAP_PATH_LOADED
    path = CONFIG.confidence_remap_path
    if not path:
        return []
    if _REMAP_CACHE is not None and _REMAP_PATH_LOADED == path:
        return _REMAP_CACHE
    if not os.path.isfile(path):
        _REMAP_CACHE = []
        _REMAP_PATH_LOADED = path
        return _REMAP_CACHE
    try:
        with open(path) as f:
            data = json.load(f)
        out = [(float(b["raw_lo"]), float(b["raw_hi"]), float(b["calibrated"]))
               for b in data.get("buckets", [])]
        out.sort(key=lambda r: r[0])
        _REMAP_CACHE = out
        _REMAP_PATH_LOADED = path
        return _REMAP_CACHE
    except Exception:
        _REMAP_CACHE = []
        _REMAP_PATH_LOADED = path
        return _REMAP_CACHE


def _remap_confidence(raw: float) -> float:
    table = _load_remap()
    if not table:
        return raw
    for lo, hi, cal in table:
        if lo <= raw < hi:
            return cal
    # Fall through: use last bucket's calibrated value if raw >= hi of last
    if raw >= table[-1][1]:
        return table[-1][2]
    if raw < table[0][0]:
        return table[0][2]
    return raw


def _apply_variant(confidence: float, state: PriceState) -> float:
    variant = CONFIG.signal_variant
    if variant == "default" or variant == "asymmetric":
        return confidence
    if variant == "calibrated":
        return _clamp(_remap_confidence(confidence), 0.0, CONFIDENCE_CAP)
    if variant == "regime_filtered":
        rv = getattr(state, "realised_vol", 0.0) or 0.0
        if rv <= 0.0:
            return confidence  # no info — pass through
        if rv < CONFIG.vol_regime_min or rv > CONFIG.vol_regime_max:
            return 0.0  # outside regime band → kill the signal
        return confidence
    return confidence


def _edge_threshold(direction: str) -> float:
    if CONFIG.signal_variant == "asymmetric":
        return CONFIG.min_edge_up if direction == "UP" else CONFIG.min_edge_down
    return CONFIG.min_edge


# --- standalone test ---

def _mk_state(**kw) -> PriceState:
    st = PriceState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _run_tests() -> None:
    base_price = 80000.0
    end_ts = time.time() + 20  # T-20s -> momentum boost 1.08x

    print("=" * 70)
    print("SIGNAL ENGINE TESTS")
    print("=" * 70)

    # Case 1: Strong UP (delta +0.10%, positive momentum + vol imbalance)
    st = _mk_state(
        current_price=base_price * 1.001,
        window_open_price=base_price,
        vwap=base_price * 1.0005,
        momentum=3.0,
        delta_from_open=0.001,
        delta_from_open_abs=0.001,
        volume_imbalance=0.4,
    )
    sig = compute_signal(st, end_ts)
    print(f"\n[strong UP] {sig.rationale}")
    print(f"  -> dir={sig.direction} conf={sig.confidence:.3f} "
          f"price=${sig.suggested_price} EV={sig.expected_value:+.4f} "
          f"trade={should_trade(sig)}")
    assert sig.direction == "UP"
    assert sig.confidence > 0.5, f"expected strong UP conf > 0.5, got {sig.confidence}"

    # Case 2: Strong DOWN
    st = _mk_state(
        current_price=base_price * 0.998,
        window_open_price=base_price,
        vwap=base_price * 0.999,
        momentum=-4.0,
        delta_from_open=-0.002,
        delta_from_open_abs=0.002,
        volume_imbalance=-0.5,
    )
    sig = compute_signal(st, end_ts)
    print(f"\n[strong DOWN] {sig.rationale}")
    print(f"  -> dir={sig.direction} conf={sig.confidence:.3f} "
          f"price=${sig.suggested_price} EV={sig.expected_value:+.4f} "
          f"trade={should_trade(sig)}")
    assert sig.direction == "DOWN"
    assert sig.confidence > 0.5, f"expected strong DOWN conf > 0.5, got {sig.confidence}"

    # Case 3: Noise — tiny delta (~0.001%), should be below MIN_CONFIDENCE
    st = _mk_state(
        current_price=base_price * 1.00001,
        window_open_price=base_price,
        vwap=base_price,
        momentum=0.0,
        delta_from_open=0.00001,
        delta_from_open_abs=0.00001,
        volume_imbalance=0.0,
    )
    sig = compute_signal(st, end_ts)
    print(f"\n[noise] {sig.rationale}")
    print(f"  -> dir={sig.direction} conf={sig.confidence:.3f} "
          f"trade={should_trade(sig)}")
    assert not should_trade(sig), "noise signal should NOT trade"

    # Case 4: Contradicting inputs — delta up but momentum strongly down
    st = _mk_state(
        current_price=base_price * 1.0005,
        window_open_price=base_price,
        vwap=base_price * 1.001,  # vwap higher than current -> vwap says down
        momentum=-5.0,
        delta_from_open=0.0005,
        delta_from_open_abs=0.0005,
        volume_imbalance=-0.3,
    )
    sig = compute_signal(st, end_ts)
    print(f"\n[contradict] {sig.rationale}")
    print(f"  -> dir={sig.direction} conf={sig.confidence:.3f}")
    # Confidence should be notably reduced by contradictions
    assert sig.confidence < 0.50, \
        f"contradicting signals should lower confidence, got {sig.confidence}"

    # Case 5: Flat — no delta, no momentum
    st = _mk_state(
        current_price=base_price,
        window_open_price=base_price,
        vwap=base_price,
        momentum=0.0,
        delta_from_open=0.0,
        delta_from_open_abs=0.0,
        volume_imbalance=0.0,
    )
    sig = compute_signal(st, end_ts)
    print(f"\n[flat] {sig.rationale}")
    print(f"  -> dir={sig.direction} conf={sig.confidence:.3f} "
          f"trade={should_trade(sig)}")
    assert not should_trade(sig), "flat signal should NOT trade"

    # Case 6: Price mapping sanity
    print("\n[price map]")
    for c in (0.55, 0.60, 0.65, 0.72, 0.80, 0.90, 0.95, 0.99):
        p = confidence_to_price(c)
        print(f"  conf={c:.2f} -> ${p}")
        assert p <= 0.95

    # Case 7: Time boost
    st_strong = _mk_state(
        current_price=base_price * 1.002,
        window_open_price=base_price,
        vwap=base_price * 1.001,
        momentum=4.0,
        delta_from_open=0.002,
        delta_from_open_abs=0.002,
        volume_imbalance=0.5,
    )
    far = compute_signal(st_strong, time.time() + 60)   # T-60, no boost
    mid = compute_signal(st_strong, time.time() + 25)   # T-25, 1.08x
    near = compute_signal(st_strong, time.time() + 10)  # T-10, 1.15x
    print(f"\n[time boost] far={far.confidence:.3f} mid={mid.confidence:.3f} near={near.confidence:.3f}")
    assert far.confidence <= mid.confidence <= near.confidence, "time boost must be monotonic"

    # Case 8: should_trade gate — too late
    late = compute_signal(st_strong, time.time() + 2)
    assert not should_trade(late), "seconds_to_close < 5 must block trade"

    print("\nAll tests PASS ✓")


if __name__ == "__main__":
    _run_tests()
