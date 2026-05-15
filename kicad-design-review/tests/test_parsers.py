"""Parser unit tests — BOM CSV, IPC-D-356A netlist, lib_symbols, schema probe.

Each test uses an inline string fixture; no on-disk schematic files needed.
"""
from __future__ import annotations

import sexpdata

import kicad_extract


# ---------------------------------------------------------------------------
# BOM CSV parser
# ---------------------------------------------------------------------------

def test_bom_basic():
    text = (
        "Designator,Footprint,Quantity,Value,LCSC Part #\n"
        '"R1, R2, R3",0201,3,1k,C270365\n'
        "U1,QFN-80,1,RP2354B,C39843328\n"
    )
    entries = kicad_extract.parse_bom(text)
    assert len(entries) == 4  # R1, R2, R3 split + U1
    refdeses = sorted(e.refdes for e in entries)
    assert refdeses == ["R1", "R2", "R3", "U1"]
    u1 = next(e for e in entries if e.refdes == "U1")
    assert u1.lcsc == "C39843328"


def test_bom_utf8_bom_in_header():
    """UTF-8 BOM (\\ufeff) at start of file shouldn't break header detection."""
    text = (
        "﻿Designator,Footprint,Value,LCSC Part #\n"
        "U1,SOT-23,LM358,C7950\n"
    )
    entries = kicad_extract.parse_bom(text)
    assert len(entries) == 1
    assert entries[0].refdes == "U1"
    assert entries[0].lcsc == "C7950"


def test_bom_no_recognizable_header():
    text = "junk,more junk,here\nrow1,row2,row3\n"
    entries = kicad_extract.parse_bom(text)
    assert entries == []


def test_bom_alternate_column_names():
    """`Reference` instead of `Designator`; `mfr part` instead of `LCSC Part #`."""
    text = (
        "Reference,Value,Mfr Part Number\n"
        "U1,LM358,LM358AN\n"
    )
    entries = kicad_extract.parse_bom(text)
    assert len(entries) == 1
    assert entries[0].refdes == "U1"
    assert entries[0].manufacturer_pn == "LM358AN"


# ---------------------------------------------------------------------------
# Positions CSV parser
# ---------------------------------------------------------------------------

def test_positions_kicad_format():
    text = (
        "Designator,Mid X,Mid Y,Rotation,Layer\n"
        "C1,71.975,-67.6,0.0,bottom\n"
        "C10,65.67,-78.48,90.0,top\n"
    )
    positions = kicad_extract.parse_positions(text)
    assert len(positions) == 2
    c1 = next(p for p in positions if p.refdes == "C1")
    assert c1.x == 71.975
    assert c1.y == -67.6
    assert c1.side == "bottom"
    c10 = next(p for p in positions if p.refdes == "C10")
    assert c10.rotation == 90.0


# ---------------------------------------------------------------------------
# IPC-D-356A netlist parser
# ---------------------------------------------------------------------------

def test_ipc_netlist_basic():
    """Component-pad records (327) — the main format we extract."""
    text = (
        "P  CODE 00\n"
        "P  UNITS CUST 0\n"
        "317NET-(D381-A)     VIA        MD0079PA00X+036506Y-028140X0138Y0000R000S3\n"
        "327+5V              U12   -1          A06X+020421Y-028518X0522Y0236R090S1\n"
        "327L_MCU/XIP_CS1N   U1    -1          A06X+026569Y-034299X0193Y0138R000S1\n"
        "327N/C              U1    -9          A06X+027049Y-034004X0079Y0630R000S1\n"
    )
    entries = kicad_extract.parse_ipc_netlist(text)
    # VIA records (317) are skipped — we only emit 327 component pads.
    # N/C records are also skipped.
    assert len(entries) == 2
    nets = sorted((e.refdes, e.pin) for e in entries)
    assert nets == [("U1", "1"), ("U12", "1")]
    u1_pin1 = next(e for e in entries if e.refdes == "U1" and e.pin == "1")
    assert u1_pin1.net == "L_MCU/XIP_CS1N"


def test_ipc_netlist_truncated_long_net_name():
    """Net names are capped at 14 characters by the IPC-D-356A standard.

    Format columns (1-indexed):
      1-3   record code (327)
      4-17  net name (14 chars, right-padded with spaces)
      18-20 padding/feature
      21-26 refdes (6 chars, left-justified, padded)
      27    '-' separator
      28-31 pin number (4 chars, left-justified)
    """
    # Net "EL_HEADER/EINT" = exactly 14 chars (the cap), at cols 4-17.
    # 3 spaces padding (18-20), then "U2" at col 21, padded to col 26,
    # then '-' at col 27, '56' starting at col 28.
    text = "327EL_HEADER/EINT   U2    -56         A06X+026569Y-034299X0193Y0138R000S1\n"
    entries = kicad_extract.parse_ipc_netlist(text)
    assert len(entries) == 1
    assert entries[0].refdes == "U2"
    assert entries[0].pin == "56"
    assert entries[0].net == "EL_HEADER/EINT"


# ---------------------------------------------------------------------------
# lib_symbols parser
# ---------------------------------------------------------------------------

def test_lib_symbols_single_unit():
    """A simple symbol with pins directly under the top-level (symbol) entry."""
    sch = sexpdata.loads('''
    (kicad_sch (version 20250114)
      (lib_symbols
        (symbol "Generic:Resistor"
          (pin passive line (at 0 2.54 270) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "1" (effects (font (size 1.27 1.27))))
          )
          (pin passive line (at 0 -2.54 90) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "2" (effects (font (size 1.27 1.27))))
          )
        )
      )
    )
    ''')
    pin_map = kicad_extract.parse_lib_symbols(sch)
    # Single-unit → both pins should land at unit 1.
    assert ("Generic:Resistor", 1, "1") in pin_map
    assert ("Generic:Resistor", 1, "2") in pin_map
    pin1 = pin_map[("Generic:Resistor", 1, "1")]
    assert pin1.primary_name == "~"
    assert pin1.pin_offset == (0.0, 2.54)


def test_lib_symbols_multi_unit_dual_op_amp():
    """Multi-unit symbol like a dual op-amp: nested sub-symbols Name_unit_body.

    Convention: <symbolname>_<unit>_<body_style>. So OPA2277_1_1 = unit 1,
    body 1; OPA2277_2_1 = unit 2, body 1.
    """
    sch = sexpdata.loads('''
    (kicad_sch (version 20250114)
      (lib_symbols
        (symbol "Amp:OPA2277"
          (symbol "OPA2277_0_1"
            (rectangle (start -5.08 5.08) (end 5.08 -5.08))
          )
          (symbol "OPA2277_1_1"
            (pin output line (at 7.62 0 180) (length 2.54)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27))))
            )
            (pin input line (at -7.62 -2.54 0) (length 2.54)
              (name "-" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27))))
            )
          )
          (symbol "OPA2277_2_1"
            (pin input line (at -7.62 2.54 0) (length 2.54)
              (name "+" (effects (font (size 1.27 1.27))))
              (number "5" (effects (font (size 1.27 1.27))))
            )
            (pin output line (at 7.62 0 180) (length 2.54)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "7" (effects (font (size 1.27 1.27))))
            )
          )
        )
      )
    )
    ''')
    pin_map = kicad_extract.parse_lib_symbols(sch)
    # Unit 1 has pins 1 (OUT) and 2 (-IN).
    assert ("Amp:OPA2277", 1, "1") in pin_map
    assert ("Amp:OPA2277", 1, "2") in pin_map
    # Unit 2 has pins 5 (+IN) and 7 (OUT).
    assert ("Amp:OPA2277", 2, "5") in pin_map
    assert ("Amp:OPA2277", 2, "7") in pin_map
    # Names are correctly resolved per unit.
    assert pin_map[("Amp:OPA2277", 1, "2")].primary_name == "-"
    assert pin_map[("Amp:OPA2277", 2, "5")].primary_name == "+"


def test_lib_symbols_pin_alternates():
    """Pin alternates (alternate function) should be collected as a list."""
    sch = sexpdata.loads('''
    (kicad_sch (version 20250114)
      (lib_symbols
        (symbol "RP2350:RP2354B_80QFN"
          (pin bidirectional line (at 0 0 0) (length 2.54)
            (name "GPIO45_ADC5" (effects (font (size 1.27 1.27))))
            (number "56" (effects (font (size 1.27 1.27))))
            (alternate "GPIO45" bidirectional line)
            (alternate "ADC5" input line)
          )
        )
      )
    )
    ''')
    pin_map = kicad_extract.parse_lib_symbols(sch)
    assert ("RP2350:RP2354B_80QFN", 1, "56") in pin_map
    pin = pin_map[("RP2350:RP2354B_80QFN", 1, "56")]
    assert pin.primary_name == "GPIO45_ADC5"
    assert sorted(pin.alternates) == ["ADC5", "GPIO45"]


def test_lib_symbols_missing():
    """Schematic with no (lib_symbols ...) block at all should return empty."""
    sch = sexpdata.loads('(kicad_sch (version 20250114))')
    pin_map = kicad_extract.parse_lib_symbols(sch)
    assert pin_map == {}


# ---------------------------------------------------------------------------
# lookup_pin_name fallbacks
# ---------------------------------------------------------------------------

def test_lookup_pin_name_exact():
    pin_map = {
        ("lib:Foo", 1, "5"): kicad_extract.SymPinDef(
            number="5", primary_name="DATA", unit=1,
        ),
    }
    pd = kicad_extract.lookup_pin_name(pin_map, "lib:Foo", 1, "5")
    assert pd is not None and pd.primary_name == "DATA"


def test_lookup_pin_name_unit_0_fallback():
    """Unit 0 = pins shared across all units; should be findable from unit 2."""
    pin_map = {
        ("lib:Foo", 0, "8"): kicad_extract.SymPinDef(
            number="8", primary_name="VCC", unit=0,
        ),
    }
    pd = kicad_extract.lookup_pin_name(pin_map, "lib:Foo", 2, "8")
    assert pd is not None and pd.primary_name == "VCC"


def test_lookup_pin_name_bare_lib_id_fallback():
    """Project-local .kicad_sym files key by bare symbol name; lookup
    should still match when the schematic uses a nickname-prefixed lib_id."""
    pin_map = {
        ("RP2354B_80QFN", 1, "56"): kicad_extract.SymPinDef(
            number="56", primary_name="GPIO45_ADC5", unit=1,
        ),
    }
    pd = kicad_extract.lookup_pin_name(
        pin_map, "panel_custom:RP2354B_80QFN", 1, "56",
    )
    assert pd is not None and pd.primary_name == "GPIO45_ADC5"


def test_lookup_pin_name_miss():
    pin_map = {
        ("lib:Foo", 1, "1"): kicad_extract.SymPinDef(
            number="1", primary_name="A", unit=1,
        ),
    }
    pd = kicad_extract.lookup_pin_name(pin_map, "lib:Bar", 1, "1")
    assert pd is None
    pd = kicad_extract.lookup_pin_name(pin_map, "lib:Foo", 1, "999")
    assert pd is None


# ---------------------------------------------------------------------------
# Source resolver
# ---------------------------------------------------------------------------

def test_parse_source_github_url():
    src = kicad_extract.parse_source(
        "https://github.com/owner/repo/tree/main/path/to/proj"
    )
    assert src.kind == "github"
    assert src.owner == "owner"
    assert src.repo == "repo"
    assert src.ref == "main"
    assert src.path == "path/to/proj"


def test_parse_source_shorthand_at_ref():
    src = kicad_extract.parse_source("owner/repo@v1.0:my/path")
    assert src.kind == "github"
    assert src.owner == "owner"
    assert src.ref == "v1.0"
    assert src.path == "my/path"


def test_parse_source_invalid():
    import pytest
    with pytest.raises(ValueError):
        kicad_extract.parse_source("not a valid source string at all")


# ---------------------------------------------------------------------------
# trace-pin arg parser
# ---------------------------------------------------------------------------

def test_parse_trace_pin_arg_basic():
    pairs = kicad_extract.parse_trace_pin_arg("U2:44,U2:56,Y1:1")
    assert pairs == [("U2", "44"), ("U2", "56"), ("Y1", "1")]


def test_parse_trace_pin_arg_empty():
    assert kicad_extract.parse_trace_pin_arg(None) == []
    assert kicad_extract.parse_trace_pin_arg("") == []


def test_parse_trace_pin_arg_whitespace_tolerant():
    pairs = kicad_extract.parse_trace_pin_arg(" U2:44 , Y1:1 ")
    assert pairs == [("U2", "44"), ("Y1", "1")]
