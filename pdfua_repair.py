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

        pdf.save(
            output_pdf_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )

        if verbose:
            print(f"Repaired widgets: {widgets_repaired}")
            print(f"Repaired links:   {links_repaired}")
            print(f"Saved: {output_pdf_path}")

        return widgets_repaired, links_repaired


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Repair PDF/UA annotation structure for Widgets and Links.")
    parser.add_argument("input_pdf", help="Input PDF path")
    parser.add_argument("output_pdf", help="Output PDF path")
    args = parser.parse_args()

    repair_pdfua_annotations(args.input_pdf, args.output_pdf, verbose=True)