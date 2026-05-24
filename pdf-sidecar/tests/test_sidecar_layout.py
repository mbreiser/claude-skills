"""Sidecar directory layout and metadata.json schema."""
from __future__ import annotations

import json
from pathlib import Path

import pdf_sidecar


def test_full_mode_layout(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    stub_backend(
        "marker",
        document_md=" ".join(f"word{i}" for i in range(120)),
        n_images=3,
        n_tables=2,
    )
    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    sidecar = Path(out["sidecar"])

    assert (sidecar / "document.md").exists()
    assert (sidecar / "metadata.json").exists()
    assert (sidecar / "images" / "figure-001.png").exists()
    assert (sidecar / "images" / "figure-002.png").exists()
    assert (sidecar / "images" / "figure-003.png").exists()
    assert (sidecar / "tables" / "table-001.md").exists()
    assert (sidecar / "tables" / "table-002.md").exists()


def test_metadata_schema(make_pdf, stub_backend, capsys):
    pdf = make_pdf(words_per_page=40)
    stub_backend(
        "marker",
        document_md=" ".join(f"word{i}" for i in range(80)),
        n_images=1,
        backend_version="9.9.9",
    )
    pdf_sidecar.main([str(pdf), "--quiet"])
    out = json.loads(capsys.readouterr().out)
    meta = json.loads(Path(out["document_md"]).parent.joinpath("metadata.json").read_text())

    # Top-level keys
    assert set(meta.keys()) >= {
        "tool", "backend", "mode", "source",
        "extracted_at", "stats", "verification",
    }
    # Tool block
    assert meta["tool"]["name"] == "pdf-sidecar"
    assert meta["tool"]["version"] == pdf_sidecar.TOOL_VERSION
    assert meta["tool"]["schema_version"] == pdf_sidecar.SCHEMA_VERSION
    # Backend block
    assert meta["backend"]["name"] == "marker"
    assert meta["backend"]["version"] == "9.9.9"
    # Mode
    assert meta["mode"] == "full"
    # Source block
    assert meta["source"]["path"] == str(pdf.resolve())
    assert len(meta["source"]["sha256"]) == 64
    assert meta["source"]["size_bytes"] > 0
    # Stats block
    assert meta["stats"]["pages"] >= 1
    assert meta["stats"]["figures"] == 1
    assert meta["stats"]["words"] > 0
    # Verification block
    assert meta["verification"]["status"] in {"ok", "warning", "fail"}
    assert "word_ratio" in meta["verification"]


def test_image_filenames_zero_padded(make_pdf, stub_backend, capsys):
    pdf = make_pdf()
    stub_backend(
        "marker",
        document_md=" ".join(f"word{i}" for i in range(120)),
        n_images=12,  # tests 1..9 and 10..12 padding
    )
    pdf_sidecar.main([str(pdf), "--quiet"])
    out = json.loads(capsys.readouterr().out)
    sidecar = Path(out["sidecar"])
    files = sorted(p.name for p in (sidecar / "images").iterdir())
    assert files[0] == "figure-001.png"
    assert files[8] == "figure-009.png"
    assert files[9] == "figure-010.png"
    assert files[-1] == "figure-012.png"


def test_sidecar_falls_back_to_cache_when_parent_readonly(
    make_pdf, stub_backend, capsys, tmp_path, monkeypatch
):
    """If the PDF's parent dir is not writable, sidecar lands in ~/.cache."""
    pdf = make_pdf()
    stub_backend("marker", document_md=" ".join(f"word{i}" for i in range(120)))

    # Force os.access to report the parent as not writable.
    real_access = pdf_sidecar.os.access

    def _patched_access(path, mode):
        if Path(path) == pdf.parent and mode == pdf_sidecar.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(pdf_sidecar.os, "access", _patched_access)

    # Pin the home dir to a tmp location so we don't pollute the real ~/.cache.
    fake_home = tmp_path / "fakehome"
    monkeypatch.setattr(pdf_sidecar.Path, "home", classmethod(lambda cls: fake_home))

    pdf_sidecar.main([str(pdf), "--quiet"])
    out = json.loads(capsys.readouterr().out)
    sidecar = Path(out["sidecar"])
    assert fake_home in sidecar.parents
    assert sidecar.name == pdf_sidecar.sha256_file(pdf)
