#!/usr/bin/env python3
"""
Structural checker for PDF/UA-1 (ISO 14289-1) Clause 7.1, Test 3:
"Content shall be marked as Artifact or tagged as real content."

Computes the rule directly (no veraPDF): every mark-producing operator on a page
must be inside either
  (a) a marked-content sequence whose MCID is referenced by a StructElem (real), or
  (b) an /Artifact marked-content scope (decoration).
Anything else is reported as UNTAGGED.

Also classifies each UNTAGGED text op by page band (header/footer margin vs body)
and decodes a sample of its string, so furniture (safe to /Artifact) is easy to
tell apart from real body text (which must stay tagged as real content).

Usage:  python diagnose_untagged.py FILE.pdf [--max-samples N]
Exit 0 if zero untagged (CI gate), 1 otherwise.
"""
import sys
import argparse
from collections import defaultdict

import pikepdf

TEXT_OPS = {"Tj", "TJ", "'", '"'}
PATH_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}

# ---- tiny 3x3 (row-vector) affine helpers: (a,b,c,d,e,f) ----
IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

def mmul(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a*a2 + b*c2, a*b2 + b*d2,
            c*a2 + d*c2, c*b2 + d*d2,
            e*a2 + f*c2 + e2, e*b2 + f*d2 + f2)

def apply_origin(m):
    # transform text-space origin (0,0) -> device (x,y)
    return m[4], m[5]


def _objgen(obj):
    try:
        return obj.objgen
    except Exception:
        return None


def collect_referenced_mcids(pdf):
    referenced = set()
    root = pdf.Root
    if "/StructTreeRoot" not in root:
        return referenced
    seen = set()

    def walk(elem, inherited_pg):
        oid = _objgen(elem)
        if oid is not None:
            if oid in seen:
                return
            seen.add(oid)
        pg = inherited_pg
        try:
            if isinstance(elem, pikepdf.Dictionary) and "/Pg" in elem:
                pg = _objgen(elem.Pg)
        except Exception:
            pass
        try:
            k = elem.K if (isinstance(elem, pikepdf.Dictionary) and "/K" in elem) else None
        except Exception:
            k = None
        if k is not None:
            _walk_k(k, pg)

    def _walk_k(k, pg):
        if isinstance(k, pikepdf.Array):
            for item in k:
                _walk_k(item, pg)
            return
        if isinstance(k, int):
            if pg is not None:
                referenced.add((pg, int(k)))
            return
        try:
            is_dict = isinstance(k, pikepdf.Dictionary)
        except Exception:
            is_dict = False
        if not is_dict:
            return
        ktype = str(k.get("/Type", "")) if "/Type" in k else ""
        if ktype == "/MCR":
            mpg = _objgen(k.Pg) if "/Pg" in k else pg
            if mpg is not None and "/MCID" in k:
                referenced.add((mpg, int(k.MCID)))
            return
        if ktype == "/OBJR":
            return
        walk(k, pg)

    walk(root.StructTreeRoot, None)
    return referenced


def _resolve_mcid(props, resources):
    try:
        if isinstance(props, pikepdf.Dictionary):
            return int(props.MCID) if "/MCID" in props else None
        if isinstance(props, pikepdf.Name) and resources is not None and "/Properties" in resources:
            pd = resources.Properties.get(props, None)
            if isinstance(pd, pikepdf.Dictionary) and "/MCID" in pd:
                return int(pd.MCID)
    except Exception:
        return None
    return None


def _decode_text(op, operands):
    """Best-effort decode of shown text bytes (eyeball only)."""
    out = []
    try:
        if op == "Tj" and operands:
            out.append(bytes(operands[0]))
        elif op == "'" and operands:
            out.append(bytes(operands[0]))
        elif op == '"' and len(operands) >= 3:
            out.append(bytes(operands[2]))
        elif op == "TJ" and operands and isinstance(operands[0], pikepdf.Array):
            for el in operands[0]:
                if isinstance(el, pikepdf.String):
                    out.append(bytes(el))
    except Exception:
        return ""
    try:
        return b"".join(out).decode("latin-1", "replace").strip()
    except Exception:
        return ""


def scan_stream(instructions, resources, page_gen, page_h, referenced, stack,
                gstate, xobject_stack, findings, counts, bands):
    """gstate = dict(ctm=..., ctm_stack=[], tm=..., tlm=..., leading=0.0)."""
    def covered():
        return any(s in ("artifact", "real") for s in stack)

    def band_for_y(y):
        if page_h <= 0:
            return "body"
        if y >= 0.90 * page_h:
            return "header"
        if y <= 0.10 * page_h:
            return "footer"
        return "body"

    idx = 0
    for item in instructions:
        idx += 1
        if isinstance(item, pikepdf.ContentStreamInlineImage):
            if not covered():
                counts["image"] += 1
                findings.append((page_gen, "image", "BI", idx, "", band_for_y(gstate["ctm"][5])))
            continue
        op = str(item.operator)
        operands = item.operands

        # --- graphics state / matrices (best effort) ---
        if op == "q":
            gstate["ctm_stack"].append(gstate["ctm"]); continue
        if op == "Q":
            if gstate["ctm_stack"]:
                gstate["ctm"] = gstate["ctm_stack"].pop()
            continue
        if op == "cm" and len(operands) == 6:
            try:
                gstate["ctm"] = mmul(tuple(float(x) for x in operands), gstate["ctm"])
            except Exception:
                pass
            continue
        if op == "BT":
            gstate["tm"] = IDENT; gstate["tlm"] = IDENT; continue
        if op == "TL" and operands:
            try: gstate["leading"] = float(operands[0])
            except Exception: pass
            continue
        if op in ("Td", "TD") and len(operands) == 2:
            try:
                tx, ty = float(operands[0]), float(operands[1])
                if op == "TD":
                    gstate["leading"] = -ty
                gstate["tlm"] = mmul((1, 0, 0, 1, tx, ty), gstate["tlm"])
                gstate["tm"] = gstate["tlm"]
            except Exception:
                pass
            continue
        if op == "Tm" and len(operands) == 6:
            try:
                gstate["tm"] = gstate["tlm"] = tuple(float(x) for x in operands)
            except Exception:
                pass
            continue
        if op == "T*":
            gstate["tlm"] = mmul((1, 0, 0, 1, 0, -gstate["leading"]), gstate["tlm"])
            gstate["tm"] = gstate["tlm"]
            # fall through: T* does not paint

        # --- marked content ---
        if op in ("BDC", "BMC"):
            tag = operands[0] if operands else None
            props = operands[1] if len(operands) > 1 else None
            if isinstance(tag, pikepdf.Name) and str(tag) == "/Artifact":
                stack.append("artifact")
            else:
                mcid = _resolve_mcid(props, resources) if op == "BDC" else None
                stack.append("real" if (mcid is not None and (page_gen, mcid) in referenced) else "other")
            continue
        if op == "EMC":
            if stack: stack.pop()
            continue

        # --- content-producing ops ---
        if op in TEXT_OPS:
            if op in ("'", '"'):  # move to next line first
                gstate["tlm"] = mmul((1, 0, 0, 1, 0, -gstate["leading"]), gstate["tlm"])
                gstate["tm"] = gstate["tlm"]
            if not covered():
                dev = mmul(gstate["tm"], gstate["ctm"])
                y = dev[5]
                band = band_for_y(y)
                counts["text"] += 1
                bands["text_" + band] += 1
                findings.append((page_gen, "text", op, idx, _decode_text(op, operands), band))
            continue
        if op in PATH_PAINT_OPS:
            if not covered():
                counts["path"] += 1
                findings.append((page_gen, "path", op, idx, "", band_for_y(gstate["ctm"][5])))
            continue
        if op == "Do":
            name = operands[0] if operands else None
            xobj = None
            try:
                if resources is not None and "/XObject" in resources and isinstance(name, pikepdf.Name):
                    xobj = resources.XObject.get(name, None)
            except Exception:
                xobj = None
            subtype = ""
            try:
                subtype = str(xobj.Subtype) if (xobj is not None and "/Subtype" in xobj) else ""
            except Exception:
                subtype = ""
            if subtype == "/Image":
                if not covered():
                    counts["image"] += 1
                    findings.append((page_gen, "image", "Do", idx, "", band_for_y(gstate["ctm"][5])))
            elif subtype == "/Form":
                xgen = _objgen(xobj)
                if xgen is not None and xgen in xobject_stack:
                    continue
                try:
                    sub = pikepdf.parse_content_stream(xobj)
                except Exception:
                    continue
                sub_res = xobj.Resources if "/Resources" in xobj else resources
                sub_g = dict(ctm=gstate["ctm"], ctm_stack=[], tm=IDENT, tlm=IDENT, leading=0.0)
                scan_stream(sub, sub_res, page_gen, page_h, referenced, list(stack),
                            sub_g, xobject_stack | ({xgen} if xgen else set()),
                            findings, counts, bands)
            continue


def diagnose(path, max_samples=25):
    pdf = pikepdf.open(path)
    referenced = collect_referenced_mcids(pdf)
    findings, counts, bands = [], defaultdict(int), defaultdict(int)
    per_page = defaultdict(lambda: defaultdict(int))

    for pnum, page in enumerate(pdf.pages):
        page_gen = _objgen(page.obj)
        try:
            mb = [float(x) for x in page.MediaBox]
            page_h = mb[3] - mb[1]
        except Exception:
            page_h = 792.0
        try:
            instr = pikepdf.parse_content_stream(page)
        except Exception as e:
            print(f"  [warn] page {pnum}: parse failed: {e}", file=sys.stderr)
            continue
        resources = page.Resources if "/Resources" in page else None
        g = dict(ctm=IDENT, ctm_stack=[], tm=IDENT, tlm=IDENT, leading=0.0)
        before = len(findings)
        scan_stream(instr, resources, page_gen, page_h, referenced, [], g, set(),
                    findings, counts, bands)
        for f in findings[before:]:
            per_page[pnum][f[1]] += 1

    total = len(findings)
    print(f"FILE: {path}")
    print(f"  pages: {len(pdf.pages)}   structure-referenced MCIDs: {len(referenced)}")
    print(f"  UNTAGGED content operators (7.1-3 violations): {total}")
    print(f"    by type: text={counts['text']}  image={counts['image']}  path={counts['path']}")
    print(f"    untagged TEXT by band: header={bands['text_header']} "
          f"footer={bands['text_footer']} body={bands['text_body']}")
    print("    ^ header/footer text = furniture (safe to /Artifact); "
          "body text = real content (must stay tagged real)")
    if per_page:
        worst = sorted(per_page.items(), key=lambda kv: -sum(kv[1].values()))[:12]
        print("  worst pages (page: text/image/path):")
        for pnum, d in worst:
            print(f"    p{pnum}: text={d['text']} image={d['image']} path={d['path']}")
    text_samples = [f for f in findings if f[1] == "text" and f[4]][:max_samples]
    if text_samples:
        print("  sample UNTAGGED text strings (band | text):")
        for f in text_samples:
            print(f"    [{f[5]:6}] {f[4][:80]!r}")
    pdf.close()
    return total, dict(counts), dict(bands)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--max-samples", type=int, default=25)
    args = ap.parse_args()
    total, _, _ = diagnose(args.pdf, args.max_samples)
    sys.exit(0 if total == 0 else 1)
