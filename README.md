# pdf-engine-crisp — PDF Accessibility Engine (CRISP worker / small-docs lane)

FastAPI service that turns an HTML representation + the original PDF into a
tagged, accessible **PDF/UA-1** output. Sibling engine: `pdf-enginev2`
(core / heavy lane). The two share `pdfua_repair.py` and the upload handling.

## Large source PDFs

Source PDFs are POSTed as multipart `file` / `original_pdf` fields to
`/get-page-count`, `/split-to-queue`, `/detect-forms`, `/detect-forms-single`,
`/build`, and `/split`. Every endpoint **streams the upload to a temp file on
disk** (`shutil.copyfileobj`) rather than reading it into memory, so large
documents do not spike RSS.

### `MAX_PDF_UPLOAD_MB`
Maximum accepted upload size, in megabytes. **Default: `600`.**

Raise it to accept larger source PDFs. It is applied to the multipart parser at
startup and is version-robust: the current Starlette pin has no hard per-part
cap, and this also overrides any per-part cap a future Starlette release might
enforce (a bare class-attribute assignment would not, because the parser sets a
per-instance default). Small PDFs behave exactly as before.

## Environment variables

| var | default | purpose |
|-----|---------|---------|
| `MAX_PDF_UPLOAD_MB` | `600` | max multipart upload size (MB) |
| `PORT` | `10000` | uvicorn listen port |
| `SUPABASE_URL` | – | Supabase project URL (job queue + built-PDF storage) |
| `SUPABASE_KEY` | – | Supabase service key |

See `.env.example`.

## Run

```
uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --timeout-keep-alive 2700
```

## Notes on the largest documents

- Upload **acceptance** is independent of the instance tier (uploads spool to
  disk). **Processing** very large / high-page-count PDFs (rasterization +
  WeasyPrint in `/build`) is CPU- and memory-intensive and may need a larger
  Render tier and adequate ephemeral disk for the transient temp files.
- The built output is uploaded to the Supabase `pdf_outputs` bucket. If that
  store rejects an oversized output, `/build` falls back to returning the PDF
  inline, so ingestion is never blocked by the store's own size cap. To store
  large built outputs, raise the bucket / project storage file-size limit in
  Supabase.
