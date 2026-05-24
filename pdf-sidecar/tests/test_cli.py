"""CLI integration: argument parsing, exit codes, idempotency, --force, --out."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pdf_sidecar


# ---------------------------------------------------------------------------
# Argparse / basic invocation
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    ns = pdf_sidecar.parse_args(["foo.pdf"])
    assert ns.pdf == "foo.pdf"
    assert ns.high_fidelity is False
    assert ns.text_only is False
    assert ns.force is False
    assert ns.out is None
    assert ns.quiet is False


def test_parse_args_all_flags():
    ns = pdf_sidecar.parse_args(
        ["paper.pdf", "--high-fidelity", "--text-only", "--force",
         "--out", "/tmp/x", "--quiet"]
    )
    assert ns.high_fidelity is True
    assert ns.text_only is True
    assert ns.force is True
    assert ns.out == Path("/tmp/x")
    assert ns.quiet is True


def test_missing_pdf_exits_2(tmp_path: Path, capsys):
    rc = pdf_sidecar.main([str(tmp_path / "does-not-exist.pdf")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_directory_as_input_exits_2(tmp_path: Path, capsys):
    rc = pdf_sidecar.main([str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a regular file" in err


# ---------------------------------------------------------------------------
# Happy path: extract → cache hit → --force
# ---------------------------------------------------------------------------


def test_fresh_extraction_emits_extracted_status(make_pdf, stub_backend, capsys):
    pdf = make_pdf(words_per_page=60)
    stub_backend(
        "marker",
        document_md=" ".join(f"word{i}" for i in range(120)),
    )

    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "extracted"
    assert out["mode"] == "full"
    sidecar = Path(out["sidecar"])
    assert sidecar.exists()
    assert (sidecar / "document.md").exists()
    assert (sidecar / "metadata.json").exists()


def test_second_invocation_hits_cache(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    stub_backend("marker", document_md=" ".join(f"word{i}" for i in range(120)))

    # First run extracts.
    rc1 = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc1 == 0
    capsys.readouterr()

    # Second run should hit cache.
    rc2 = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc2 == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "cached"


def test_force_re_extracts_even_on_cache_hit(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    stub_backend("marker", document_md=" ".join(f"word{i}" for i in range(120)))
    pdf_sidecar.main([str(pdf), "--quiet"])
    capsys.readouterr()

    rc = pdf_sidecar.main([str(pdf), "--force", "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "extracted"


def test_hash_mismatch_re_extracts(make_pdf, stub_backend, capsys, tmp_path):
    """If the source PDF changes, the cache must be invalidated."""
    pdf = make_pdf("v1.pdf")
    stub_backend("marker", document_md=" ".join(f"word{i}" for i in range(120)))
    pdf_sidecar.main([str(pdf), "--quiet"])
    capsys.readouterr()

    # Overwrite the PDF with new content (different sha).
    pdf2 = make_pdf("v1.pdf", words_per_page=80)
    assert pdf2 == pdf  # same path
    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "extracted"


# ---------------------------------------------------------------------------
# --out redirection
# ---------------------------------------------------------------------------


def test_out_flag_redirects_sidecar(make_pdf, stub_backend, capsys, tmp_path):
    pdf = make_pdf()
    stub_backend("marker", document_md=" ".join(f"word{i}" for i in range(120)))
    out_dir = tmp_path / "custom" / "sidecar"

    rc = pdf_sidecar.main([str(pdf), "--out", str(out_dir), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert Path(out["sidecar"]) == out_dir.resolve()
    assert (out_dir / "document.md").exists()


# ---------------------------------------------------------------------------
# Backend crash maps to exit 3
# ---------------------------------------------------------------------------


def test_backend_crash_exits_3(make_pdf, stub_backend, capsys):
    pdf = make_pdf()

    # Stub raises BackendFailure when invoked.
    stub_backend("marker", raise_exc=pdf_sidecar.BackendFailure)
    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 3
