"""Image-link rewriting: Marker emits markdown with refs to its internal
filenames; we rename the files and need to rewrite the markdown so the
links actually resolve in the sidecar.
"""
from __future__ import annotations

import pdf_sidecar


def test_rewrites_marker_style_refs_to_images_subdir():
    name_map = {
        "_page_0_Picture_33.jpeg": "figure-001.png",
        "_page_3_Figure_1.jpeg": "figure-002.png",
    }
    md = (
        "Title\n\n"
        "![](_page_0_Picture_33.jpeg)\n\n"
        "Some text\n\n"
        "![](_page_3_Figure_1.jpeg)\n"
    )
    out = pdf_sidecar._rewrite_image_refs(md, name_map, subdir="images")
    assert "(images/figure-001.png)" in out
    assert "(images/figure-002.png)" in out
    # Original refs should be gone.
    assert "_page_0_Picture_33.jpeg" not in out
    assert "_page_3_Figure_1.jpeg" not in out


def test_empty_name_map_returns_markdown_unchanged():
    md = "Some markdown with no images."
    out = pdf_sidecar._rewrite_image_refs(md, {}, subdir="images")
    assert out == md


def test_only_replaces_inside_image_ref_syntax():
    """A token that happens to appear in body text shouldn't be rewritten.

    We anchor the replacement on `](orig)` (the closing paren after
    markdown's empty alt text), which is sufficient for Marker's output.
    Bare mentions of the filename in prose should NOT be touched.
    """
    name_map = {"foo.jpeg": "figure-001.png"}
    md = "Body mentions foo.jpeg in passing. But ![](foo.jpeg) is real."
    out = pdf_sidecar._rewrite_image_refs(md, name_map, subdir="images")
    # The image ref got rewritten.
    assert "![](images/figure-001.png)" in out
    # The prose mention is untouched.
    assert "mentions foo.jpeg in passing" in out


def test_pkg_version_falls_back_when_package_missing():
    """importlib.metadata.version raises PackageNotFoundError for unknown pkgs."""
    assert pdf_sidecar._pkg_version("definitely-not-a-real-package-xyz") == "unknown"
