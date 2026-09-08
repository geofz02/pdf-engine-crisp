"""Regression test for PDF/UA-1 (ISO 14289-1) Clause 7.1, Test 3:
"Content shall be marked as Artifact or tagged as real content."

Uses the same structural checker the team runs on real outputs
(tools/diagnose_untagged.py) as the oracle:

  * a path-only furniture fixture (mirrors the real failing sample: real content
    tagged with MCIDs + untagged decorative path-paint ops) must go to ZERO
    untagged after the completeness pass, with the structure tree intact;
  * the pass must be idempotent;
  * the pass must NEVER wrap text in /Artifact (real content can't be hidden):
    a fixture with untagged body text keeps that text untagged (not artifacted)
    after the pass.

Run:  pytest tests/  (or)  python tests/test_untagged_content.py
Requires: pikepdf.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, os.path.join(_ROOT, "tools"), _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import pikepdf  # noqa: E402
import make_fixture  # noqa: E402
import diagnose_untagged as diag  # noqa: E402
from pdfua_repair import (  # noqa: E402
    apply_content_completeness,
    mark_untagged_paths_as_artifacts,
)


def _untagged(path):
    total, counts, bands = diag.diagnose(path)
    return total, counts


def _ref_count(path):
    with pikepdf.open(path) as pdf:
        return len(diag.collect_referenced_mcids(pdf))


def test_path_furniture_goes_to_zero_and_keeps_structure():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.pdf")
        out = os.path.join(d, "out.pdf")
        make_fixture.make_path_only(src)

        before, bcounts = _untagged(src)
        assert before > 0, "fixture should start with untagged content"
        assert bcounts.get("text", 0) == 0 and bcounts.get("image", 0) == 0, \
            "fixture furniture must be path-only (as in the real sample)"

        assert apply_content_completeness(src, out) is True

        after, acounts = _untagged(out)
        assert after == 0, f"expected 0 untagged after pass, got {after}"
        # structure tree must be untouched (no real content converted/dropped)
        assert _ref_count(src) == _ref_count(out) > 0


def test_pass_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.pdf")
        out = os.path.join(d, "out.pdf")
        make_fixture.make_path_only(src)
        apply_content_completeness(src, out)
        with pikepdf.open(out) as pdf:
            second = mark_untagged_paths_as_artifacts(pdf)
        assert second == 0, "re-running the pass must change nothing"


def test_never_artifacts_real_text():
    """Guardrail: untagged body text must remain real content (never hidden)."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.pdf")
        out = os.path.join(d, "out.pdf")
        make_fixture.make_mixed(src)

        before, bcounts = _untagged(src)
        assert bcounts.get("text", 0) > 0 and bcounts.get("path", 0) > 0

        apply_content_completeness(src, out)
        after, acounts = _untagged(out)

        # paths get wrapped...
        assert acounts.get("path", 0) == 0
        # ...but the untagged text is left exactly as-is (NOT artifacted / hidden)
        assert acounts.get("text", 0) == bcounts.get("text", 0) > 0


if __name__ == "__main__":
    test_path_furniture_goes_to_zero_and_keeps_structure()
    test_pass_is_idempotent()
    test_never_artifacts_real_text()
    print("OK: all 7.1-3 completeness regression tests passed")
