"""Verification thresholds and the fail-→-exit-6-no-sidecar contract."""
from __future__ import annotations

import json
from pathlib import Path

import pdf_sidecar


# ---------------------------------------------------------------------------
# Unit tests of verify()
# ---------------------------------------------------------------------------


def _baseline(words: int = 100, images: int = 0):
    return pdf_sidecar.Baseline(
        pages=1,
        chars=words * 5,
        expected_words=words,
        expected_images=images,
        is_scanned=False,
    )


def test_verify_ok_high_ratio():
    v = pdf_sidecar.verify(
        baseline=_baseline(100), actual_words=95, actual_images=0, mode="text-only"
    )
    assert v.status == "ok"
    assert v.word_ratio >= 0.85


def test_verify_warning_mid_ratio():
    v = pdf_sidecar.verify(
        baseline=_baseline(100), actual_words=70, actual_images=0, mode="text-only"
    )
    assert v.status == "warning"


def test_verify_fail_low_ratio():
    v = pdf_sidecar.verify(
        baseline=_baseline(100), actual_words=10, actual_images=0, mode="text-only"
    )
    assert v.status == "fail"


def test_verify_fail_zero_words_on_nonempty_pdf():
    v = pdf_sidecar.verify(
        baseline=_baseline(100), actual_words=0, actual_images=0, mode="text-only"
    )
    assert v.status == "fail"


def test_verify_image_deficit_downgrades_to_warning():
    """Even with great word_ratio, missing images in full mode → warning."""
    v = pdf_sidecar.verify(
        baseline=_baseline(100, images=10),
        actual_words=100,
        actual_images=2,  # 20% of expected — well below 0.7 threshold
        mode="full",
    )
    assert v.status == "warning"


def test_verify_image_deficit_ignored_in_text_only_mode():
    """In text-only mode, image deficit doesn't matter."""
    v = pdf_sidecar.verify(
        baseline=_baseline(100, images=10),
        actual_words=100,
        actual_images=0,
        mode="text-only",
    )
    assert v.status == "ok"


def test_env_thresholds_override(monkeypatch):
    """User-tunable thresholds via env vars."""
    monkeypatch.setenv("PDF_SIDECAR_WORD_RATIO_OK", "0.99")
    v = pdf_sidecar.verify(
        baseline=_baseline(100), actual_words=95, actual_images=0, mode="text-only"
    )
    # 0.95 ratio is now below ok threshold of 0.99.
    assert v.status == "warning"


# ---------------------------------------------------------------------------
# End-to-end: fail status must NOT commit a sidecar (exit 6)
# ---------------------------------------------------------------------------


def test_verification_fail_does_not_commit_sidecar(
    make_pdf, stub_backend, capsys
):
    """When verify() returns status='fail', the sidecar dir must not exist."""
    pdf = make_pdf(words_per_page=80)  # baseline ~160 words across 2 pages

    # Stub returns just a handful of words — well below the fail threshold.
    stub_backend("marker", document_md="only five words here ok")

    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 6

    # No sidecar dir committed.
    sidecar = Path(str(pdf) + ".extracted")
    assert not sidecar.exists()


def test_verification_warning_still_commits_sidecar(
    make_pdf, stub_backend, capsys
):
    """Warning status keeps the sidecar but records the warning."""
    pdf = make_pdf(words_per_page=80)

    # ~50% of source — lands in warning band but not fail.
    stub_backend(
        "marker", document_md=" ".join(f"word{i}" for i in range(85))
    )

    rc = pdf_sidecar.main([str(pdf), "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    sidecar = Path(out["sidecar"])
    assert sidecar.exists()
    meta = json.loads((sidecar / "metadata.json").read_text())
    assert meta["verification"]["status"] == "warning"
    assert meta["verification"]["notes"]
