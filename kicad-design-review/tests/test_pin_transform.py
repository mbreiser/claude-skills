"""Pin-coordinate transform tests — 12 cases covering rotation × mirror.

These verify the formula in absolute_pin_position(): a pin's absolute
coordinate on the schematic given its symbol-instance position, the
symbol's rotation, mirror flag, and the pin's symbol-local offset.

Cases: rotation ∈ {0, 90, 180, 270} × mirror ∈ {"", "x", "y"} = 12 total.

KiCad's mirror semantics:
  (mirror x) — flip across the X axis: y → -y in symbol-local frame
  (mirror y) — flip across the Y axis: x → -x in symbol-local frame
Mirror is applied BEFORE rotation; that's the order we model.
"""
from __future__ import annotations

import math

import kicad_extract  # loaded by conftest


def almost_equal(a: tuple[float, float], b: tuple[float, float],
                 tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


# A consistent reference setup: symbol placed at (10, 20), pin offset (3, 0).
SYM_ORIGIN = (10.0, 20.0)
PIN_OFFSET = (3.0, 0.0)


def test_rotation_0_no_mirror():
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 0.0, "", PIN_OFFSET,
    )
    # No transform → just translate by symbol_origin.
    assert almost_equal(abs_pos, (13.0, 20.0))


def test_rotation_90_no_mirror():
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 90.0, "", PIN_OFFSET,
    )
    # (3, 0) rotated 90° CCW → (0, 3); + (10, 20) → (10, 23).
    assert almost_equal(abs_pos, (10.0, 23.0))


def test_rotation_180_no_mirror():
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 180.0, "", PIN_OFFSET,
    )
    # (3, 0) rotated 180° → (-3, 0); + (10, 20) → (7, 20).
    assert almost_equal(abs_pos, (7.0, 20.0))


def test_rotation_270_no_mirror():
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 270.0, "", PIN_OFFSET,
    )
    # (3, 0) rotated 270° → (0, -3); + (10, 20) → (10, 17).
    assert almost_equal(abs_pos, (10.0, 17.0))


def test_rotation_0_mirror_x():
    # Pin offset (3, 0) — y is 0, so mirror x changes nothing for this pin.
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 0.0, "x", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (13.0, 20.0))


def test_rotation_0_mirror_y():
    # Mirror y: x → -x in symbol-local frame; (3, 0) → (-3, 0); + origin → (7, 20).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 0.0, "y", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (7.0, 20.0))


def test_rotation_90_mirror_x():
    # Mirror x: (3, 0) → (3, 0) (y was 0). Then rotate 90° CCW → (0, 3). + origin → (10, 23).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 90.0, "x", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (10.0, 23.0))


def test_rotation_90_mirror_y():
    # Mirror y: (3, 0) → (-3, 0). Rotate 90° CCW → (0, -3). + origin → (10, 17).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 90.0, "y", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (10.0, 17.0))


def test_rotation_180_mirror_x():
    # Mirror x: (3, 0) → (3, 0). Rotate 180° → (-3, 0). + origin → (7, 20).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 180.0, "x", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (7.0, 20.0))


def test_rotation_180_mirror_y():
    # Mirror y: (3, 0) → (-3, 0). Rotate 180° → (3, 0). + origin → (13, 20).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 180.0, "y", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (13.0, 20.0))


def test_rotation_270_mirror_x():
    # Mirror x: (3, 0) → (3, 0). Rotate 270° CCW → (0, -3). + origin → (10, 17).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 270.0, "x", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (10.0, 17.0))


def test_rotation_270_mirror_y():
    # Mirror y: (3, 0) → (-3, 0). Rotate 270° CCW → (0, 3). + origin → (10, 23).
    abs_pos = kicad_extract.absolute_pin_position(
        SYM_ORIGIN, 270.0, "y", PIN_OFFSET,
    )
    assert almost_equal(abs_pos, (10.0, 23.0))


def test_mirror_x_with_nonzero_y():
    """Mirror with a pin offset that has both nonzero x and y — verifies y-flip."""
    abs_pos = kicad_extract.absolute_pin_position(
        (0.0, 0.0), 0.0, "x", (5.0, 7.0),
    )
    # mirror x flips y: (5, 7) → (5, -7); + origin (0,0) = (5, -7).
    assert almost_equal(abs_pos, (5.0, -7.0))


def test_mirror_y_with_nonzero_y():
    abs_pos = kicad_extract.absolute_pin_position(
        (0.0, 0.0), 0.0, "y", (5.0, 7.0),
    )
    # mirror y flips x: (5, 7) → (-5, 7).
    assert almost_equal(abs_pos, (-5.0, 7.0))
