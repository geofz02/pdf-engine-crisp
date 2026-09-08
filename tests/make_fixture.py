#!/usr/bin/env python3
"""Deterministic fixtures for the 7.1-3 regression test.

make_path_only(path):  mirrors the real failing sample -> real tagged content
    (body text MCID0, figure MCID1) PLUS untagged path-paint furniture only
    (background fill + rule stroke per page). No untagged text/images.
make_mixed(path):  additionally includes an UNTAGGED body-text op, to prove the
    completeness pass never wraps text in /Artifact (guardrail).
"""
import sys
import pikepdf
from pikepdf import Pdf, Dictionary, Array, Name, String, Stream


def _build(path, include_untagged_text):
    pdf = Pdf.new()
    img = Stream(pdf, b"\x80")
    img.Type = Name.XObject; img.Subtype = Name.Image
    img.Width = 1; img.Height = 1
    img.ColorSpace = Name.DeviceGray; img.BitsPerComponent = 8
    img_ref = pdf.make_indirect(img)
    font = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))

    def page(content):
        res = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=img_ref))
        cs = pdf.make_indirect(Stream(pdf, content))
        return pdf.make_indirect(Dictionary(
            Type=Name.Page, MediaBox=Array([0, 0, 612, 792]),
            Resources=res, Contents=cs))

    # real tagged content (MCID0 text, MCID1 figure) + untagged path furniture
    body = b"""
/P <</MCID 0>> BDC
BT /F1 12 Tf 72 700 Td (Real body paragraph.) Tj ET
EMC
/Figure <</MCID 1>> BDC
q 100 0 0 100 72 500 cm /Im0 Do Q
EMC
1 1 1 rg 0 0 612 792 re f
0 0 0 RG 72 90 m 540 90 l S
"""
    if include_untagged_text:
        body += b"BT /F1 12 Tf 200 400 Td (Untagged body sentence that must not be hidden.) Tj ET\n"

    p1 = page(body)
    p2 = page(body)
    pages = pdf.make_indirect(Dictionary(Type=Name.Pages, Kids=Array([p1, p2]), Count=2))
    p1.Parent = pages; p2.Parent = pages
    pdf.Root.Pages = pages

    sroot = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=sroot))
    kids = []
    for pg in (p1, p2):
        kids.append(pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.P, P=doc, Pg=pg, K=0)))
        kids.append(pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Figure, P=doc, Pg=pg, K=1, Alt=String("Figure"))))
    doc.K = Array(kids)
    sroot.K = Array([doc])
    pdf.Root.StructTreeRoot = sroot
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.save(path)


def make_path_only(path):
    _build(path, include_untagged_text=False)


def make_mixed(path):
    _build(path, include_untagged_text=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "path_only"
    out = sys.argv[2] if len(sys.argv) > 2 else which + ".pdf"
    (make_path_only if which == "path_only" else make_mixed)(out)
    print("wrote", out)
