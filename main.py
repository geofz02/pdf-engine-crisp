from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from weasyprint import HTML
import fitz  # PyMuPDF
import os
import shutil
import uuid
import base64
import logging
import re
import json
import gc  # Garbage Collector
import time
from datetime import datetime

# --- CRITICAL FASTAPI 1MB LIMIT FIX ---
import starlette.formparsers
starlette.formparsers.MultiPartParser.max_part_size = 100 * 1024 * 1024

# --- V4 SUPABASE ENGINE IMPORT ---
from supabase import create_client, Client

# --- AI / FORM DETECTION IMPORT ---
from commonforms import prepare_form

# --- PDF/UA POST-REPAIR IMPORT ---
from pdfua_repair import repair_pdfua_annotations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Silence Weasyprint info logs to save CPU
logging.getLogger('weasyprint').setLevel(logging.ERROR)
logging.getLogger('fontTools').setLevel(logging.ERROR)

app = FastAPI(title="MediA11y Enterprise PDF - CRISP WORKER")

# =================================================================
# UTILITY FUNCTIONS
# =================================================================

def cleanup_files(*paths):
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Cleaned up: {path}")
            except Exception as e:
                logger.error(f"Error cleaning up {path}: {str(e)}")

def parse_pdf_date(pdf_date):
    if not pdf_date or not str(pdf_date).startswith("D:"):
        return datetime.utcnow().isoformat() + "Z"
    try:
        clean_date = str(pdf_date)[2:].replace("'", "")
        iso_str = (
            f"{clean_date[0:4]}-{clean_date[4:6]}-{clean_date[6:8]}"
            f"T{clean_date[8:10]}:{clean_date[10:12]}:{clean_date[12:14]}"
        )
        if len(clean_date) > 14:
            iso_str += f"{clean_date[14:17]}:{clean_date[17:19]}"
        else:
            iso_str += "Z"
        return iso_str
    except Exception:
        return datetime.utcnow().isoformat() + "Z"

def escape_html(value: str) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

def safe_field_name(name: str, fallback: str) -> str:
    raw = (name or fallback).replace("&quot;", '"')
    raw = re.sub(r"[^\w\s\-]", " ", raw)
    raw = re.sub(r"\s+", "_", raw).strip("_")
    raw = re.sub(r"_+", "_", raw)
    return raw or fallback

def labelize_field_name(name: str) -> str:
    if not name:
        return "Form Field"
    label = name.replace("_", " ").strip()
    label = re.sub(r"\s+", " ", label)
    return label or "Form Field"

def normalize_forms_payload(corrected_forms_json: str):
    if not corrected_forms_json:
        return {}
    try:
        parsed = json.loads(corrected_forms_json)
    except Exception:
        return {}

    if isinstance(parsed, dict) and "forms" in parsed and isinstance(parsed["forms"], dict):
        parsed = parsed["forms"]
    if not isinstance(parsed, dict):
        return {}

    normalized = {}
    valid_types = {0, 1, 2, 3, 4, 5, 6, 7}

    for page_key, fields in parsed.items():
        if not isinstance(fields, list):
            continue
        normalized[str(page_key)] = []
        for idx, f in enumerate(fields):
            if not isinstance(f, dict):
                continue
            try:
                x0, y0 = float(f.get("x0", 0)), float(f.get("y0", 0))
                x1, y1 = float(f.get("x1", 0)), float(f.get("y1", 0))
            except Exception:
                continue

            if x1 < x0: x0, x1 = x1, x0
            if y1 < y0: y0, y1 = y1, y0

            width, height = x1 - x0, y1 - y0

            if width < 8 or height < 8: continue
            if width > 750 or height > 1000: continue

            try:
                field_type = int(f.get("type", 7))
            except Exception:
                field_type = 7

            if field_type not in valid_types:
                field_type = 2 if (width <= 18 and height <= 18) else 7

            raw_name = str(f.get("name") or f"Field_Pg{page_key}_{idx + 1}")
            final_name = safe_field_name(raw_name, f"Field_Pg{page_key}_{idx + 1}")
            final_label = labelize_field_name(final_name)

            normalized[str(page_key)].append({
                "x0": round(x0, 2), "y0": round(y0, 2),
                "x1": round(x1, 2), "y1": round(y1, 2),
                "type": field_type, "name": final_name, "label": final_label,
            })
    return normalized

def strip_duplicate_leading_title(html_content: str, document_title: str) -> str:
    if not html_content or not document_title:
        return html_content
    normalized_title = re.sub(r"\s+", " ", document_title).strip().lower()
    patterns = [r"^\s*<h1[^>]*>(.*?)</h1>", r"^\s*<h2[^>]*>(.*?)</h2>", r"^\s*<p[^>]*>(.*?)</p>"]
    cleaned = html_content
    for pattern in patterns:
        m = re.search(pattern, cleaned, flags=re.IGNORECASE | re.DOTALL)
        if not m: continue
        first_text = re.sub(r"<[^>]+>", " ", m.group(1))
        first_text = re.sub(r"\s+", " ", first_text).strip().lower()
        if first_text == normalized_title:
            cleaned = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE | re.DOTALL).lstrip()
            break
    return cleaned

def build_form_control(page_index: int, field_index: int, f: dict) -> str:
    x0, y0, x1, y1 = f["x0"], f["y0"], f["x1"], f["y1"]
    width = max(1, round(x1 - x0, 2))
    height = max(1, round(y1 - y0, 2))
    field_type = int(f["type"])
    field_name = escape_html(f["name"])
    field_label = escape_html(f["label"])
    field_id = f"fld_{page_index}_{field_index}"

    base_style = f"position:absolute; left:{x0}pt; top:{y0}pt; width:{width}pt; height:{height}pt; z-index:95;"

    if field_type == 2 or field_type == 5:
        size = min(width, height)
        return f'<input id="{field_id}" class="pdf-form" type="text" name="{field_name}" title="{field_label} (Type X to select)" aria-label="{field_label} (Type X to select)" style="{base_style} width:{size}pt; height:{size}pt; text-align: center; font-size: {size*0.75}pt;" />'
    else:
        return f'<input id="{field_id}" class="pdf-form" type="text" name="{field_name}" value="" title="{field_label}" aria-label="{field_label}" style="{base_style}" />'

# =================================================================
# HEALTH CHECK
# =================================================================

@app.get("/healthz")
def health_check():
    return {"status": "online", "version": "78.0.0-CRISP-200DPI-FIXED"}

# =================================================================
# ENDPOINT: INSTANT PAGE COUNTER (FOR N8N ROUTING)
# =================================================================

@app.post("/get-page-count")
async def get_page_count(file: UploadFile = File(...)):
    try:
        temp_pdf = f"/tmp/count_{uuid.uuid4()}.pdf"
        with open(temp_pdf, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        doc = fitz.open(temp_pdf)
        pages = len(doc)
        doc.close()
        os.remove(temp_pdf)
        
        return JSONResponse(content={"total_pages": pages})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# =================================================================
# ENDPOINT: DETECT FORMS
# =================================================================

@app.post("/detect-forms")
async def detect_forms(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    request_id = str(uuid.uuid4())
    temp_pdf = f"/tmp/temp_detect_orig_{request_id}.pdf"
    try:
        with open(temp_pdf, "wb") as f:
            shutil.copyfileobj(file.file, f)
        doc_orig = fitz.open(temp_pdf)
        total_pages = len(doc_orig)
        doc_orig.close()
        final_forms = {}
        files_to_cleanup = [temp_pdf]
        for i in range(total_pages):
            final_forms[str(i)] = []
            page_in = f"/tmp/temp_page_in_{request_id}_{i}.pdf"
            page_out = f"/tmp/temp_page_out_{request_id}_{i}.pdf"
            files_to_cleanup.extend([page_in, page_out])
            single_page_doc = fitz.open(temp_pdf)
            single_page_doc.select([i])
            single_page_doc.save(page_in)
            single_page_doc.close()
            native_page_forms = []
            check_doc = fitz.open(page_in)
            widgets = check_doc[0].widgets()
            if widgets:
                for w in widgets:
                    native_page_forms.append({
                        "x0": round(w.rect.x0, 2), "y0": round(w.rect.y0, 2),
                        "x1": round(w.rect.x1, 2), "y1": round(w.rect.y1, 2),
                        "type": int(w.field_type),
                        "name": safe_field_name(w.field_name, f"Field_Pg{i}_{len(native_page_forms) + 1}")
                    })
            check_doc.close()
            if len(native_page_forms) > 0:
                final_forms[str(i)] = native_page_forms
            else:
                ai_page_forms = []
                try:
                    prepare_form(page_in, page_out)
                    out_doc = fitz.open(page_out)
                    out_widgets = out_doc[0].widgets()
                    if out_widgets:
                        for w in out_widgets:
                            ai_page_forms.append({
                                "x0": round(w.rect.x0, 2), "y0": round(w.rect.y0, 2),
                                "x1": round(w.rect.x1, 2), "y1": round(w.rect.y1, 2),
                                "type": int(w.field_type),
                                "name": safe_field_name(w.field_name, f"Field_Pg{i}_{len(ai_page_forms) + 1}")
                            })
                    out_doc.close()
                    final_forms[str(i)] = ai_page_forms
                except Exception:
                    pass
            gc.collect()
        background_tasks.add_task(cleanup_files, *files_to_cleanup)
        return JSONResponse(content={"total_pages": total_pages, "forms": final_forms})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/detect-forms-single")
async def detect_forms_single(background_tasks: BackgroundTasks, page_index: int = Form(...), file: UploadFile = File(...)):
    request_id = str(uuid.uuid4())
    temp_pdf = f"/tmp/temp_detect_{request_id}.pdf"
    page_in = f"/tmp/temp_page_in_{request_id}.pdf"
    page_out = f"/tmp/temp_page_out_{request_id}.pdf"
    files_to_cleanup = [temp_pdf, page_in, page_out]
    try:
        with open(temp_pdf, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        doc_orig = fitz.open(temp_pdf)
        if page_index >= len(doc_orig):
            doc_orig.close()
            return JSONResponse(status_code=400, content={"error": "Page index out of bounds"})
        doc_orig.select([page_index])
        doc_orig.save(page_in)
        doc_orig.close()
        native_page_forms = []
        check_doc = fitz.open(page_in)
        widgets = check_doc[0].widgets()
        if widgets:
            for w in widgets:
                native_page_forms.append({
                    "x0": round(w.rect.x0, 2), "y0": round(w.rect.y0, 2),
                    "x1": round(w.rect.x1, 2), "y1": round(w.rect.y1, 2),
                    "type": int(w.field_type),
                    "name": safe_field_name(w.field_name, f"Field_Pg{page_index}_{len(native_page_forms) + 1}")
                })
        check_doc.close()
        final_forms = []
        if len(native_page_forms) > 0:
            final_forms = native_page_forms
        else:
            ai_page_forms = []
            try:
                prepare_form(page_in, page_out)
                out_doc = fitz.open(page_out)
                out_widgets = out_doc[0].widgets()
                if out_widgets:
                    for w in out_widgets:
                        ai_page_forms.append({
                            "x0": round(w.rect.x0, 2), "y0": round(w.rect.y0, 2),
                            "x1": round(w.rect.x1, 2), "y1": round(w.rect.y1, 2),
                            "type": int(w.field_type),
                            "name": safe_field_name(w.field_name, f"Field_Pg{page_index}_{len(ai_page_forms) + 1}")
                        })
                out_doc.close()
                final_forms = ai_page_forms
            except Exception:
                pass
        gc.collect()
        background_tasks.add_task(cleanup_files, *files_to_cleanup)
        return JSONResponse(content={str(page_index): final_forms})
    except Exception as e:
        cleanup_files(*files_to_cleanup)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/split-to-queue")
async def split_pdf_to_queue(file: UploadFile = File(...), original_pdf_url: str = Form(...)):
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    if not SUPABASE_URL or not SUPABASE_KEY:
        return JSONResponse(status_code=500, content={"error": "Supabase keys missing."})
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        temp_pdf = f"/tmp/split_q_{uuid.uuid4()}.pdf"
        with open(temp_pdf, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        doc = fitz.open(temp_pdf)
        meta = doc.metadata
        document_title = meta.get("title") or file.filename or "Accessible Document"
        total_pages = len(doc)
        job_response = supabase.table("pdf_jobs").insert({
            "document_title": document_title,
            "total_pages": total_pages,
            "status": "processing",
            "original_pdf_url": original_pdf_url
        }).execute()
        job_id = job_response.data[0]["id"]
        pages_to_insert = []
        for i in range(total_pages):
            page = doc.load_page(i)
            blocks = page.get_text("blocks")
            page_text = "\n".join([re.sub(r"MediaAlly|MediaA11y", "MediA11y", b[4]) for b in blocks if len(b) > 6 and b[6] == 0])
            
            # --- 150 DPI FOR CRISPER AI TEXT RECOGNITION ---
            pix = page.get_pixmap(dpi=150)
            b64_img = base64.b64encode(pix.tobytes("jpeg", 90)).decode("utf-8")
            
            pages_to_insert.append({
                "job_id": job_id, "page_index": i, "page_text": page_text,
                "base64_image": f"data:image/jpeg;base64,{b64_img}", "status": "pending",
                "original_pdf_url": original_pdf_url
            })
            if len(pages_to_insert) >= 10:
                supabase.table("pdf_pages").insert(pages_to_insert).execute()
                pages_to_insert.clear()
                gc.collect()
        if len(pages_to_insert) > 0:
            supabase.table("pdf_pages").insert(pages_to_insert).execute()
        doc.close()
        os.remove(temp_pdf)
        gc.collect()
        return {"message": "Queue Populated", "job_id": job_id, "total_pages": total_pages, "document_title": document_title}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# =================================================================
# ENDPOINT: MASTER BUILD
# =================================================================

@app.post("/build")
async def build_pdf(
    background_tasks: BackgroundTasks,
    html_content: str = Form(...),
    corrected_forms_json: str = Form("{}"),
    original_pdf: UploadFile = File(...),
    document_title: str = Form(...),
    pdf_keywords: str = Form("")
):
    req_id = str(uuid.uuid4())
    orig_path = f"/tmp/temp_orig_{req_id}.pdf"
    weasy_out_path = f"/tmp/weasy_out_{req_id}.pdf"
    intermediate_path = f"/tmp/post_meta_{req_id}.pdf"
    final_path = f"/tmp/master_{req_id}.pdf"
    temp_image_paths = []

    # The magic 1x1 transparent GIF to force WeasyPrint to generate a clickable link box
    transparent_gif = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

    try:
        with open(orig_path, "wb") as f:
            shutil.copyfileobj(original_pdf.file, f)

        doc_orig = fitz.open(orig_path)
        visual_count = len(doc_orig)
        orig_meta = doc_orig.metadata or {}

        pdf_title = document_title if document_title and document_title.lower() != "document" else (orig_meta.get("title") or "Accessible Document")
        pdf_author = orig_meta.get("author") or ""
        pdf_subject = orig_meta.get("subject")
        if not pdf_subject or pdf_subject.strip() == "":
            pdf_subject = pdf_title
        final_keywords = pdf_keywords if pdf_keywords.strip() else orig_meta.get("keywords", "Accessibility, PDF/UA")
        pdf_creator = "MediA11y"
        pdf_producer = "MediA11y"
        pdf_creation_date = orig_meta.get("creationDate") or fitz.get_pdf_now()

        ai_forms = normalize_forms_payload(corrected_forms_json)
        html_content = strip_duplicate_leading_title(html_content, pdf_title)

        visual_html_blocks = []
        dynamic_css = []
        all_doc_links = set()

        for i in range(visual_count):
            page_orig = doc_orig[i]
            w, h = page_orig.rect.width, page_orig.rect.height
            dynamic_css.append(f"@page page_{i} {{ size: {w}pt {h}pt; margin: 0; }}")
            
            img_path = f"/tmp/img_{req_id}_{i}.jpg"
            temp_image_paths.append(img_path)
            
            # --- CRITICAL FIX: MEMORY-SAFE CRISP RESOLUTION ---
            # 200 DPI / 85% Quality prevents the 512MB RAM Crash while maintaining crisp visuals
            pix = page_orig.get_pixmap(dpi=250)
            img_data = pix.tobytes("jpeg", 85)
            with open(img_path, "wb") as img_file:
                img_file.write(img_data)
            del img_data
            pix = None
            gc.collect()

            block = []
            block.append(f'<div id="page_{i}" class="visual-page" style="page: page_{i}; position: relative; width: {w}pt; height: {h}pt; page-break-after: always; overflow: hidden; background-color: white;">')
            block.append(f'<img src="file://{img_path}" alt="Visual representation of page {i + 1}" style="position: absolute; top: 0; left: 0; width: {w}pt; height: {h}pt; z-index: 1;" />')

            # Layer 2: Clickable Links (Native)
            for link in page_orig.get_links():
                if "uri" in link and link["uri"]:
                    all_doc_links.add(link["uri"])
                    rect = link["from"]
                    lw, lh = round(rect.x1 - rect.x0, 2), round(rect.y1 - rect.y0, 2)
                    href = escape_html(link["uri"])
                    block.append(f'<a href="{href}" title="{href}" aria-label="Link" style="display:block; position:absolute; left:{round(rect.x0, 2)}pt; top:{round(rect.y0, 2)}pt; width:{lw}pt; height:{lh}pt; z-index:90;"><img src="{transparent_gif}" alt="Link" style="width:100%; height:100%; border:none; outline:none; margin:0; padding:0;" /></a>')

            # Layer 2.5: Plain-text URL Scanner
            words = page_orig.get_text("words")
            for w_data in words:
                text = w_data[4]
                if "http://" in text or "https://" in text or "www." in text:
                    match = re.search(r'(https?://[^\s]+|www\.[^\s]+)', text)
                    if match:
                        raw_url = match.group(1).rstrip('.,;:"\'()<>[]')
                        full_url = raw_url if raw_url.startswith("http") else "https://" + raw_url
                        x0, y0, x1, y1 = w_data[0], w_data[1], w_data[2], w_data[3]
                        lw, lh = x1 - x0, y1 - y0
                        href = escape_html(full_url)
                        all_doc_links.add(full_url)
                        block.append(f'<a href="{href}" title="{href}" style="display:block; position:absolute; left:{round(x0, 2)}pt; top:{round(y0, 2)}pt; width:{round(lw, 2)}pt; height:{round(lh, 2)}pt; z-index:90;"><img src="{transparent_gif}" alt="Link" style="width:100%; height:100%; border:none; outline:none; margin:0; padding:0;" /></a>')
                if "@" in text and "." in text:
                    email_match = re.search(r'([\w\.\-]+@[\w\.\-]+\.[a-zA-Z]{2,4})', text)
                    if email_match:
                        raw_email = email_match.group(1)
                        full_url = "mailto:" + raw_email
                        x0, y0, x1, y1 = w_data[0], w_data[1], w_data[2], w_data[3]
                        lw, lh = x1 - x0, y1 - y0
                        href = escape_html(full_url)
                        all_doc_links.add(full_url)
                        block.append(f'<a href="{href}" title="{href}" style="display:block; position:absolute; left:{round(x0, 2)}pt; top:{round(y0, 2)}pt; width:{round(lw, 2)}pt; height:{round(lh, 2)}pt; z-index:90;"><img src="{transparent_gif}" alt="Link" style="width:100%; height:100%; border:none; outline:none; margin:0; padding:0;" /></a>')

            # Layer 3: Forms
            for field_index, field in enumerate(ai_forms.get(str(i), [])):
                block.append(build_form_control(i, field_index, field))
            
            # Layer 4: Footer Badge
            block.append(f'<div style="position: absolute; bottom: 15pt; right: 15pt; background-color: #ffffff; z-index: 100; border: 1pt solid #000000; padding: 4pt 8pt; display: block; border-radius: 4pt;" aria-hidden="true"><div style="text-align: center; font-family: Arial, sans-serif; font-size: 10pt; font-weight: bold; color: #000000;">Visual Page {i + 1}</div></div>')
            
            block.append('</div>')
            visual_html_blocks.append("".join(block))

        doc_orig.close()

        links_appendix = ""
        if all_doc_links:
            links_appendix = "<div style='margin-top: 40px; border-top: 2px solid #ccc; padding-top: 20px;'><h2>Document Links Referenced</h2><ul>"
            for uri in sorted(all_doc_links):
                safe_uri = escape_html(uri)
                links_appendix += f'<li><a href="{safe_uri}">{safe_uri}</a></li>'
            links_appendix += "</ul></div>"

        visual_pages_toc = "Page 1" if visual_count == 1 else f"Pages 1-{visual_count}"
        guide_page = visual_count + 1
        text_start = visual_count + 2

        master_html = f"""
<!DOCTYPE html>
<html lang="en-US">
<head>
    <meta charset="utf-8">
    <title>{escape_html(pdf_title)}</title>
    <meta name="author" content="{escape_html(pdf_author)}">
    <meta name="description" content="{escape_html(pdf_subject)}">
    <meta name="keywords" content="{escape_html(final_keywords)}">
    <style>
        body {{ font-family: Arial, Helvetica, sans-serif; font-size: 12pt; margin: 0; color: #000; line-height: 1.6; }}
        {chr(10).join(dynamic_css)}
        
        @page toc {{ 
            size: letter portrait; 
            margin: 1in; 
            @bottom-center {{ content: "Accessibility text version Page " counter(page); font-family: Arial, Helvetica, sans-serif; font-size: 10pt; }} 
        }}
        @page text_pages {{ 
            size: letter portrait; 
            margin: 1in; 
            @bottom-center {{ content: "Accessibility text version Page " counter(page); font-family: Arial, Helvetica, sans-serif; font-size: 10pt; }} 
        }}
        
        .toc-page {{ page: toc; page-break-after: always; }}
        .text-section {{ page: text_pages; }}
        a {{ color: #0000EE; text-decoration: underline; font-weight: bold; }}
        h1, h2 {{ color: #000; border: none; }}
        
        .pdf-form {{ box-sizing: border-box; background: rgba(255,255,255,0.01); border: 0.75pt solid rgba(0, 0, 120, 0.75); }}
    </style>
</head>
<body>
    {''.join(visual_html_blocks)}

    <div class="toc-page">
        <h1>Accessibility Remediation Guide</h1>
        <p>This document has been enhanced for universal accessibility.</p>
        <h2>Table of Contents</h2>
        <ul>
            <li><a href="#page_0">Section 1: Interactive Original Pages</a> ({visual_pages_toc})</li>
            <li><a href="#remediation-info">Section 2: Remediation Information</a> (Page {guide_page})</li>
            <li><a href="#accessible-text">Section 3: Accessible Narrative</a> ([TEXT_PAGES_TOC_PLACEHOLDER])</li>
        </ul>
        <div id="remediation-info" style="margin-top: 50px; border-top: 1px solid #eee;">
            <p><strong>Remediated by:</strong> MediA11y</p>
        </div>
    </div>

    <div id="accessible-text" class="text-section">
        <h1 id="accessible-text-header">{escape_html(pdf_title)}</h1>
        {html_content}
        {links_appendix}
    </div>
</body>
</html>
"""
        del visual_html_blocks
        del dynamic_css
        gc.collect()

        temp_weasy_doc = HTML(string=master_html).render()
        total_pages = len(temp_weasy_doc.pages)
        toc_text = f"(Page {text_start})" if total_pages == text_start else f"(Pages {text_start}-{total_pages})"
        final_master_html = master_html.replace("([TEXT_PAGES_TOC_PLACEHOLDER])", toc_text)
        
        del temp_weasy_doc
        del master_html
        gc.collect()

        HTML(string=final_master_html).write_pdf(target=weasy_out_path, pdf_variant="pdf/ua-1", pdf_forms=True)

        pdf_out = fitz.open(weasy_out_path)

        metadata_final = {
            "title": str(pdf_title or ""),
            "author": str(pdf_author or ""),
            "subject": str(pdf_subject or ""),
            "keywords": str(final_keywords or ""),
            "creator": str(pdf_creator),
            "producer": str(pdf_producer),
            "creationDate": str(pdf_creation_date or fitz.get_pdf_now()),
            "modDate": fitz.get_pdf_now()
        }
        pdf_out.set_metadata(metadata_final)
        pdf_out.set_language("en-US")

        for i in range(len(pdf_out)):
            page_xref = pdf_out.page_xref(i)
            try:
                pdf_out.xref_set_key(page_xref, "Tabs", "/S")
            except Exception: pass

        pdf_out.save(intermediate_path, garbage=3, deflate=True)
        pdf_out.close()

        repair_pdfua_annotations(input_pdf_path=intermediate_path, output_pdf_path=final_path, verbose=True)

        safe_filename = re.sub(r'[\\/:*?"<>|]+', " ", pdf_title).strip() or "accessible-document"

        # --- SUPABASE UPLOAD LOGIC ---
        SUPABASE_URL = os.getenv("SUPABASE_URL")
        SUPABASE_KEY = os.getenv("SUPABASE_KEY")
        
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
                bucket_name = "pdf_outputs"
                unique_filename = f"{safe_filename}_{str(uuid.uuid4())[:8]}.pdf"
                
                with open(final_path, "rb") as f:
                    supabase.storage.from_(bucket_name).upload(
                        path=unique_filename,
                        file=f,
                        file_options={"content-type": "application/pdf"}
                    )
                
                public_url = supabase.storage.from_(bucket_name).get_public_url(unique_filename)
                
                background_tasks.add_task(cleanup_files, orig_path, weasy_out_path, intermediate_path, final_path, *temp_image_paths)
                
                return JSONResponse(content={
                    "status": "success", 
                    "message": "PDF built and uploaded to Supabase",
                    "pdf_url": public_url
                })
            except Exception as upload_error:
                logger.error(f"Supabase Upload Failed: {str(upload_error)}")
                background_tasks.add_task(cleanup_files, orig_path, weasy_out_path, intermediate_path, *temp_image_paths)
                return FileResponse(path=final_path, filename=f"{safe_filename}.pdf", media_type="application/pdf")
        else:
            background_tasks.add_task(cleanup_files, orig_path, weasy_out_path, intermediate_path, *temp_image_paths)
            return FileResponse(path=final_path, filename=f"{safe_filename}.pdf", media_type="application/pdf")

    except Exception as e:
        logger.error(f"MASTER BUILD CRITICAL FAILURE: {str(e)}")
        cleanup_files(orig_path, weasy_out_path, intermediate_path, *temp_image_paths)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/split")
async def split_pdf_legacy(file: UploadFile = File(...)):
    try:
        content = await file.read()
        doc = fitz.open(stream=content, filetype="pdf")
        meta = doc.metadata
        metadata_payload = {
            "title": meta.get("title") or "Accessible Document",
            "author": meta.get("author") or "MediA11y",
            "creationDate": parse_pdf_date(meta.get("creationDate"))
        }

        chunks = []
        for i in range(len(doc)):
            page = doc.load_page(i)
            blocks = page.get_text("blocks")
            page_text = "\n".join([re.sub(r"MediaAlly|MediaA11y", "MediA11y", b[4]) for b in blocks if len(b) > 6 and b[6] == 0])
            links = [link["uri"] for link in page.get_links() if "uri" in link and link["uri"]]
            pix = page.get_pixmap(dpi=120)
            b64_img = base64.b64encode(pix.tobytes("jpeg", 80)).decode("utf-8")

            chunks.append({
                "chunk_index": i, "text": page_text,
                "hidden_links": list(set(links)), "images": [b64_img]
            })
        doc.close()
        return {"metadata": metadata_payload, "total_chunks": len(chunks), "chunks": chunks}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})