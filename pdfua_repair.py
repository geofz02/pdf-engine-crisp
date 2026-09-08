import re
from typing import Optional, Tuple

import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array, String


def _pdf_name(value: str) -> Name:
    if value.startswith("/"):
        return Name(value)
    return Name("/" + value)


def _normalize_label(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _annotation_label(annot: Dictionary, subtype: str, fallback_index: int) -> str:
    for key in (Name.TU, Name.T, Name.Contents):
        try:
            if key in annot and str(annot[key]).strip():
                return _normalize_label(str(annot[key]))
        except Exception:
            pass

    if subtype == "/Widget":
        return f"Form Field {fallback_index}"
    if subtype == "/Link":
        return f"Link {fallback_index}"
    return f"Annotation {fallback_index}"


def _get_struct_tree_root(pdf: Pdf) -> Dictionary:
    root = pdf.Root
    if Name.StructTreeRoot in root and isinstance(root[Name.StructTreeRoot], Dictionary):
        return root[Name.StructTreeRoot]

    struct_root = pdf.make_indirect(Dictionary(
        Type=Name.StructTreeRoot,
        K=Array(),
    ))
    root[Name.StructTreeRoot] = struct_root
    return struct_root


def _ensure_parent_tree(pdf: Pdf, struct_root: Dictionary) -> Dictionary:
    if Name.ParentTree in struct_root and isinstance(struct_root[Name.ParentTree], Dictionary):
        parent_tree = struct_root[Name.ParentTree]
    else:
        parent_tree = pdf.make_indirect(Dictionary(
            Nums=Array()
        ))
        struct_root[Name.ParentTree] = parent_tree

    if Name.Nums not in parent_tree or not isinstance(parent_tree[Name.Nums], Array):
        parent_tree[Name.Nums] = Array()

    if Name.ParentTreeNextKey not in struct_root:
        nums = parent_tree[Name.Nums]
        max_key = -1
        for i in range(0, len(nums), 2):
            try:
                k = int(nums[i])
                if k > max_key:
                    max_key = k
            except Exception:
                continue
        struct_root[Name.ParentTreeNextKey] = max_key + 1

    return parent_tree


def _append_kid(parent_elem: Dictionary, child_obj):
    if Name.K not in parent_elem:
        parent_elem[Name.K] = child_obj
        return

    current = parent_elem[Name.K]
    if isinstance(current, Array):
        current.append(child_obj)
    else:
        parent_elem[Name.K] = Array([current, child_obj])


def _append_parent_tree_num(parent_tree: Dictionary, key_num: int, value_obj):
    nums = parent_tree[Name.Nums]
    nums.append(key_num)
    nums.append(value_obj)


def _get_or_make_root_k_array(struct_root: Dictionary) -> Array:
    if Name.K not in struct_root:
        struct_root[Name.K] = Array()
        return struct_root[Name.K]

    k = struct_root[Name.K]
    if isinstance(k, Array):
        return k

    new_k = Array([k])
    struct_root[Name.K] = new_k
    return new_k


def _build_struct_elem(pdf: Pdf, parent_elem, std_type: Name, page_obj, annot_obj, alt_text: Optional[str] = None):
    objr = pdf.make_indirect(Dictionary(
        Type=Name.OBJR,
        Obj=annot_obj,
        Pg=page_obj
    ))

    elem_dict = Dictionary(
        Type=Name.StructElem,
        S=std_type,
        P=parent_elem,
        Pg=page_obj,
        K=objr,
    )
    if alt_text:
        elem_dict[Name.Alt] = String(alt_text)

    elem = pdf.make_indirect(elem_dict)
    return elem, objr


def _ensure_document_root_elem(pdf: Pdf, struct_root: Dictionary):
    root_k = _get_or_make_root_k_array(struct_root)

    for item in root_k:
        try:
            if isinstance(item, Dictionary) and item.get(Name.S) == Name.Document:
                return item
        except Exception:
            pass

    doc_elem = pdf.make_indirect(Dictionary(
        Type=Name.StructElem,
        S=Name.Document,
        P=struct_root,
        K=Array(),
    ))
    root_k.append(doc_elem)
    return doc_elem


# ===========================================================================
# CONTENT-COMPLETENESS PASS  (PDF/UA-1 / ISO 14289-1, Clause 7.1, Test 3)
# ---------------------------------------------------------------------------
# "Content shall be marked as Artifact or tagged as real content."
#
# Some builds leave a small number of decorative path-paint operators (e.g. a
# per-page background/rule fill emitted by the HTML->PDF renderer) OUTSIDE any
# marked-content scope. This pass finds each mark-producing PATH-PAINT operator
# that is not already inside an /Artifact or a structure-referenced (MCID) scope
# and wraps its whole path object in an /Artifact marked-content sequence.
#
# Guardrails:
#   * Only PATH-PAINT operators are ever wrapped. Text-showing and image
#     operators are NEVER touched, so real content can never be hidden.
#   * Content is never dropped or reordered; the whole path object
#     (construction + clip + paint) is wrapped so the path object is never split.
#   * Idempotent: paths already inside a scope are left alone, so re-running is
#     a no-op.
#   * Fail-safe: callers guard with try/except; on any error the pre-pass
#     content is kept unchanged.
# ===========================================================================

_PATH_CONSTRUCT = {"m", "l", "c", "v", "y", "re", "h"}
_PATH_CLIP = {"W", "W*"}
_PATH_PAINT_MARK = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}  # produce marks
_PATH_PAINT_NOMARK = {"n"}  # end path / clip only -> no mark, never wrapped


def _objgen(obj):
    try:
        return obj.objgen
    except Exception:
        return None


def _collect_referenced_mcids(pdf: Pdf) -> set:
    """(page_objgen, mcid) pairs that the structure tree actually references."""
    referenced = set()
    root = pdf.Root
    if Name.StructTreeRoot not in root:
        return referenced
    seen = set()

    def walk(elem, pg):
        oid = _objgen(elem)
        if oid is not None:
            if oid in seen:
                return
            seen.add(oid)
        try:
            if isinstance(elem, Dictionary) and "/Pg" in elem:
                pg = _objgen(elem.Pg)
        except Exception:
            pass
        try:
            k = elem.K if (isinstance(elem, Dictionary) and "/K" in elem) else None
        except Exception:
            k = None
        if k is not None:
            walk_k(k, pg)

    def walk_k(k, pg):
        if isinstance(k, Array):
            for it in k:
                walk_k(it, pg)
            return
        if isinstance(k, int):
            if pg is not None:
                referenced.add((pg, int(k)))
            return
        if not isinstance(k, Dictionary):
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


def _resolve_mcid_props(props, resources):
    try:
        if isinstance(props, Dictionary):
            return int(props.MCID) if "/MCID" in props else None
        if isinstance(props, Name) and resources is not None and "/Properties" in resources:
            pd = resources.Properties.get(props, None)
            if isinstance(pd, Dictionary) and "/MCID" in pd:
                return int(pd.MCID)
    except Exception:
        return None
    return None


def _rewrite_instructions(instructions, resources, page_gen, referenced, pdf, visited):
    """Return (new_instruction_list, changed). Wraps untagged path objects in
    /Artifact; recurses into Form XObjects (modifying them in place)."""
    CSI = pikepdf.ContentStreamInstruction
    OP = pikepdf.Operator
    out = []
    stack = []          # scope kinds: 'artifact' | 'real' | 'other'
    path_buf = None     # tokens of the path object currently being built
    changed = False

    def covered():
        return any(s in ("artifact", "real") for s in stack)

    for item in instructions:
        if isinstance(item, pikepdf.ContentStreamInlineImage):
            if path_buf is not None:
                out.extend(path_buf); path_buf = None
            out.append(item)            # image: never wrapped
            continue

        op = str(item.operator)

        if op in _PATH_CONSTRUCT or op in _PATH_CLIP:
            if path_buf is None:
                path_buf = []
            path_buf.append(item)
            continue

        if op in _PATH_PAINT_MARK or op in _PATH_PAINT_NOMARK:
            buf = path_buf if path_buf is not None else []
            buf.append(item)
            path_buf = None
            if op in _PATH_PAINT_MARK and not covered():
                out.append(CSI([Name("/Artifact")], OP("BMC")))
                out.extend(buf)
                out.append(CSI([], OP("EMC")))
                changed = True
            else:
                out.extend(buf)
            continue

        # any non-path operator ends any pending path object defensively
        if path_buf is not None:
            out.extend(path_buf); path_buf = None

        if op in ("BDC", "BMC"):
            tag = item.operands[0] if item.operands else None
            props = item.operands[1] if len(item.operands) > 1 else None
            if isinstance(tag, Name) and str(tag) == "/Artifact":
                stack.append("artifact")
            else:
                mcid = _resolve_mcid_props(props, resources) if op == "BDC" else None
                stack.append("real" if (mcid is not None and (page_gen, mcid) in referenced) else "other")
            out.append(item)
            continue

        if op == "EMC":
            if stack:
                stack.pop()
            out.append(item)
            continue

        if op == "Do":
            name = item.operands[0] if item.operands else None
            try:
                xobj = (resources.XObject.get(name, None)
                        if (resources is not None and "/XObject" in resources and isinstance(name, Name))
                        else None)
            except Exception:
                xobj = None
            try:
                if xobj is not None and "/Subtype" in xobj and str(xobj.Subtype) == "/Form":
                    if _process_form_xobject(xobj, resources, page_gen, referenced, pdf, visited):
                        changed = True
            except Exception:
                pass
            out.append(item)
            continue

        # text-showing ops and everything else: emitted unchanged (never wrapped)
        out.append(item)

    if path_buf is not None:
        out.extend(path_buf)
    return out, changed


def _process_form_xobject(xobj, page_resources, page_gen, referenced, pdf, visited):
    oid = _objgen(xobj)
    if oid is not None:
        if oid in visited:
            return False
        visited.add(oid)
    try:
        instrs = pikepdf.parse_content_stream(xobj)
    except Exception:
        return False
    res = xobj.Resources if "/Resources" in xobj else page_resources
    new_instrs, changed = _rewrite_instructions(instrs, res, page_gen, referenced, pdf, visited)
    if changed:
        try:
            xobj.write(pikepdf.unparse_content_stream(new_instrs))
        except Exception:
            return False
    return changed


def mark_untagged_paths_as_artifacts(pdf: Pdf, verbose: bool = False) -> int:
    """Wrap every untagged path-paint operator (page content + Form XObjects) in
    an /Artifact scope. Returns the number of pages modified. Idempotent."""
    referenced = _collect_referenced_mcids(pdf)
    visited = set()
    pages_changed = 0
    for page in pdf.pages:
        try:
            pageobj = page.obj
            page_gen = _objgen(pageobj)
            instrs = pikepdf.parse_content_stream(page)
            resources = pageobj.get("/Resources", None)
            new_instrs, changed = _rewrite_instructions(
                instrs, resources, page_gen, referenced, pdf, visited)
            if changed:
                data = pikepdf.unparse_content_stream(new_instrs)
                contents = pageobj.get("/Contents", None)
                if isinstance(contents, pikepdf.Stream):
                    contents.write(data)
                else:
                    pageobj.Contents = pdf.make_stream(data)
                pages_changed += 1
        except Exception as e:
            if verbose:
                print(f"  [completeness] page skipped (fail-safe): {e}")
            continue
    if verbose:
        print(f"Content-completeness: wrapped untagged paths on {pages_changed} page(s)")
    return pages_changed


def apply_content_completeness(input_pdf_path: str, output_pdf_path: str,
                               verbose: bool = False) -> bool:
    """Standalone entry (used by tests/CLI). Fail-safe: on any error, copies the
    input to the output unchanged so a good build is never broken."""
    try:
        with Pdf.open(input_pdf_path) as pdf:
            mark_untagged_paths_as_artifacts(pdf, verbose=verbose)
            pdf.save(output_pdf_path,
                     object_stream_mode=pikepdf.ObjectStreamMode.disable)
        return True
    except Exception as e:
        if verbose:
            print(f"completeness pass failed, keeping input unchanged: {e}")
        import shutil
        if input_pdf_path != output_pdf_path:
            shutil.copyfile(input_pdf_path, output_pdf_path)
        return False


def repair_pdfua_annotations(
    input_pdf_path: str,
    output_pdf_path: str,
    verbose: bool = True,
) -> Tuple[int, int]:
    with Pdf.open(input_pdf_path) as pdf:
        struct_root = _get_struct_tree_root(pdf)
        parent_tree = _ensure_parent_tree(pdf, struct_root)
        document_elem = _ensure_document_root_elem(pdf, struct_root)

        next_key = int(struct_root.get(Name.ParentTreeNextKey, 0))

        widgets_repaired = 0
        links_repaired = 0

        for page_index, page in enumerate(pdf.pages):
            page_obj = page.obj

            page_obj[Name.Tabs] = Name.S

            annots = page_obj.get(Name.Annots, None)
            if not annots or not isinstance(annots, Array):
                continue

            for annot_index, annot in enumerate(annots):
                if not isinstance(annot, Dictionary):
                    continue

                subtype = annot.get(Name.Subtype, None)
                if subtype not in (Name.Widget, Name.Link):
                    continue

                label = _annotation_label(annot, str(subtype), annot_index + 1)

                if subtype == Name.Widget:
                    annot[Name.TU] = String(label)

                    if Name.Parent in annot and isinstance(annot[Name.Parent], Dictionary):
                        parent_field = annot[Name.Parent]
                        parent_field[Name.TU] = String(label)
                        if Name.T not in parent_field and Name.T in annot:
                            parent_field[Name.T] = annot[Name.T]

                    annot[Name.StructParent] = next_key

                    form_elem, _objr = _build_struct_elem(
                        pdf=pdf,
                        parent_elem=document_elem,
                        std_type=Name.Form,
                        page_obj=page_obj,
                        annot_obj=annot,
                        alt_text=label,
                    )

                    _append_kid(document_elem, form_elem)
                    _append_parent_tree_num(parent_tree, next_key, form_elem)

                    next_key += 1
                    widgets_repaired += 1
                    continue

                if subtype == Name.Link:
                    if Name.Contents not in annot or not str(annot.get(Name.Contents, "")).strip():
                        annot[Name.Contents] = String(label)

                    annot[Name.StructParent] = next_key

                    link_elem, _objr = _build_struct_elem(
                        pdf=pdf,
                        parent_elem=document_elem,
                        std_type=Name.Link,
                        page_obj=page_obj,
                        annot_obj=annot,
                        alt_text=label,
                    )

                    _append_kid(document_elem, link_elem)
                    _append_parent_tree_num(parent_tree, next_key, link_elem)

                    next_key += 1
                    links_repaired += 1
                    continue

        struct_root[Name.ParentTreeNextKey] = next_key

        # Final content-completeness pass (PDF/UA-1 7.1 Test 3): wrap any
        # untagged decorative path-paint operators in /Artifact. Fail-safe:
        # a failure here must never break an otherwise-good build.
        paths_pages = 0
        try:
            paths_pages = mark_untagged_paths_as_artifacts(pdf, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"Content-completeness pass skipped (fail-safe): {e}")

        pdf.save(
            output_pdf_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )

        if verbose:
            print(f"Repaired widgets: {widgets_repaired}")
            print(f"Repaired links:   {links_repaired}")
            print(f"Artifact-wrapped pages: {paths_pages}")
            print(f"Saved: {output_pdf_path}")

        return widgets_repaired, links_repaired


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Repair PDF/UA annotation structure for Widgets and Links.")
    parser.add_argument("input_pdf", help="Input PDF path")
    parser.add_argument("output_pdf", help="Output PDF path")
    args = parser.parse_args()

    repair_pdfua_annotations(args.input_pdf, args.output_pdf, verbose=True)