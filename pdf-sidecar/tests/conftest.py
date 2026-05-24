"""Make `bin/pdf-sidecar.py` importable as `pdf_sidecar` and provide PDF fixtures.

The extractor lives at bin/pdf-sidecar.py — the dash blocks `import pdf-sidecar`,
so we load via importlib and register under `pdf_sidecar`.

Synthetic PDFs for tests are generated at runtime via reportlab so we don't
check binary fixtures into the repo.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_THIS_DIR = Path(__file__).resolve().parent
_BIN_PATH = _THIS_DIR.parent / "bin" / "pdf-sidecar.py"


def _load_extractor():
    spec = importlib.util.spec_from_file_location("pdf_sidecar", _BIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pdf_sidecar"] = module
    spec.loader.exec_module(module)
    return module


# Eagerly load so test modules can `import pdf_sidecar` cleanly.
_load_extractor()


@pytest.fixture
def make_pdf(tmp_path: Path):
    """Factory: build a synthetic PDF with a controlled word count.

    Uses reportlab. Each page draws `words_per_page` distinct tokens so
    pypdf's text extraction recovers a predictable count. By default the
    factory writes a 2-page PDF with ~50 words per page.
    """
    from reportlab.pdfgen import canvas

    def _make(
        name: str = "test.pdf",
        n_pages: int = 2,
        words_per_page: int = 50,
    ) -> Path:
        path = tmp_path / name
        c = canvas.Canvas(str(path))
        for page_idx in range(n_pages):
            y = 800
            line_buf: list[str] = []
            for w in range(words_per_page):
                line_buf.append(f"p{page_idx + 1}word{w + 1}")
                if len(line_buf) == 8:
                    c.drawString(50, y, " ".join(line_buf))
                    line_buf = []
                    y -= 16
            if line_buf:
                c.drawString(50, y, " ".join(line_buf))
            c.showPage()
        c.save()
        return path

    return _make


@pytest.fixture
def stub_backend(monkeypatch):
    """Replace one or both backends with a stub for the duration of the test.

    Usage:

        def test_x(stub_backend, ...):
            stub = stub_backend("marker", document_md="hello world ...", n_images=2)
            ...
            # the script will route to `stub` instead of the real marker call
    """
    import pdf_sidecar  # noqa

    def _stub(
        backend_name: str,
        *,
        document_md: str = "stub markdown\n",
        n_images: int = 0,
        n_tables: int = 0,
        backend_version: str = "stub-1.0",
        raise_exc: type[BaseException] | None = None,
    ):
        def _fake(pdf_path, mode):
            if raise_exc is not None:
                raise raise_exc("stub backend forced failure")
            images = [
                (f"figure-{i + 1:03d}.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
                for i in range(n_images if mode == "full" else 0)
            ]
            tables = [
                (f"table-{i + 1:03d}.md", f"| col |\n|---|\n| row{i} |\n")
                for i in range(n_tables if mode == "full" else 0)
            ]
            return pdf_sidecar.BackendResult(
                document_md=document_md,
                images=images,
                tables_md=tables,
                backend_name=backend_name,
                backend_version=backend_version,
            )

        new_backends = dict(pdf_sidecar.BACKENDS)
        new_backends[backend_name] = _fake
        monkeypatch.setattr(pdf_sidecar, "BACKENDS", new_backends)
        return _fake

    return _stub
