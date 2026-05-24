"""Backend dispatch: default → marker; --high-fidelity → docling."""
from __future__ import annotations

import json

import pdf_sidecar


def test_default_routes_to_marker(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    marker_stub = stub_backend(
        "marker",
        document_md=" ".join(f"word{i}" for i in range(120)),
        backend_version="marker-stub-1",
    )
    # Sentinel: docling stub should NOT be called.
    docling_stub = stub_backend(
        "docling", raise_exc=AssertionError, backend_version="docling-stub"
    )

    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # Metadata should record marker as the chosen backend.
    import json as _json
    from pathlib import Path

    meta = _json.loads((Path(out["sidecar"]) / "metadata.json").read_text())
    assert meta["backend"]["name"] == "marker"
    assert meta["backend"]["version"] == "marker-stub-1"


def test_high_fidelity_routes_to_docling(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    stub_backend(
        "docling",
        document_md=" ".join(f"word{i}" for i in range(120)),
        backend_version="docling-stub-2",
    )
    # If marker is reached, the test fails.
    stub_backend("marker", raise_exc=AssertionError, backend_version="marker-stub")

    rc = pdf_sidecar.main([str(pdf), "--high-fidelity", "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    from pathlib import Path

    meta = json.loads((Path(out["sidecar"]) / "metadata.json").read_text())
    assert meta["backend"]["name"] == "docling"
    assert meta["backend"]["version"] == "docling-stub-2"


def test_missing_backend_in_dispatch_table_raises_argerror(monkeypatch, make_pdf, capsys):
    """If somehow BACKENDS is missing the requested key, exit 5."""
    pdf = make_pdf()
    monkeypatch.setattr(pdf_sidecar, "BACKENDS", {})  # empty dispatch table
    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 5
