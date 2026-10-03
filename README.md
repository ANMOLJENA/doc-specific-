# DOC Specific OCR Pipeline

Prototype for organizing an unknown bundle of document images into logical documents and searchable, source-linked fields. Medical documents are sample data, not hardcoded categories.

## See the prototype

From `E:\DOC-Specific-OCR-Pipeline`:

```powershell
docker compose up --build -d
```

Open [http://localhost:8000](http://localhost:8000) to use the React document-ingestion page and test real PaddleOCR manually:

1. Click **Select Image** for one photo, or choose a PDF. It previews locally without uploading or starting OCR. Choose **PaddleOCR** (default) or **Surya 2**, then click **Trigger Extraction**. The live timer starts only when extraction is triggered.
2. For several photos, click **Select bundle**, choose the files in one picker (Ctrl/Shift-click on Windows), choose the OCR engine for the whole bundle, then click **Trigger Extraction**. A bundle is one customer upload of mixed images, not an already classified document.
3. A PDF is expanded page-by-page (maximum 10 pages). Pages with selectable text use PyMuPDF directly; image-only/scanned pages are rendered to PNG and continue through the quality gate and selected OCR engine. Click **View OCR** in the run table or select a page in **Inspect Original & OCR Text** to compare the image with extracted text. The page labels the engine that produced the saved text. Use **Open original at full size** for closer inspection.

### Primary PaddleOCR and Surya fallback

The PaddleOCR CPU worker starts with normal Docker Compose. To start it separately from the project folder:

```powershell
docker compose up -d --build paddle-worker
```

The upload form lets you choose the engine before triggering extraction. **PaddleOCR** is the default: if it fails, times out (default 180 seconds per page), or returns no text, that page uses Surya 2 and is flagged for review. A low model score alone does **not** switch engines. Choosing **Surya 2** runs Surya directly without trying Paddle. The choice is saved for the whole upload and retained on retry. The API exposes the requested engine and the actual page `ocr_method` (`paddle`, `surya`, or `surya_fallback`). Existing saved cases remain as they were. Surya pages can still request a separate Paddle comparison; this never rewrites saved OCR. PP-OCRv6 weights persist in the E: `models/paddlex` folder. Only quality-gate-approved images reach either engine. No hosted OCR API is used.

For this first manual test, start with one or a few non-sensitive images; the current upload limit is 10. A single image still runs through OCR and then the AI grouping/classification stage, so it can receive a document category. The existing worker still uses Redis internally for dispatch. Multi-image uploads use internal quality-check batches, then the same AI stage groups pages into logical documents.

For the local interface preview started without Docker, use [http://localhost:8080](http://localhost:8080). In that preview, uploads save original images and show `awaiting_services`; the sample case demonstrates proposed grouping and fields.

The API schema is at [http://localhost:8000/docs](http://localhost:8000/docs).

The React frontend has [document ingestion](http://localhost:8000/), a live [ingestion queue](http://localhost:8000/queue), a searchable [document database](http://localhost:8000/database), [validation review](http://localhost:8000/validation), and [local exports](http://localhost:8000/exports). The queue and database use the read-only `GET /cases` index and display stored bundles, original thumbnails, OCR text, quality decisions, engine metadata, and real service health. Repository filters, table/card views, linked-bundle inspection, pagination, and CSV export run in the browser. Administrative controls that do not have backend endpoints (pause, prioritize, re-index) are intentionally disabled rather than simulated. The current bundle ID is carried between the ingestion, validation, and export pages.

## React frontend

The source is in `frontend/`. FastAPI serves the Vite production build from `app/static/react/`. To update the UI after editing React source:

```powershell
cd E:\DOC-Specific-OCR-Pipeline\frontend
npm ci
npm run build
```

Then refresh [http://localhost:8000/](http://localhost:8000/). The build is served by the existing API container through the project bind mount; no separate frontend server is required. For hot-reload development, run `npm run dev` in `frontend/` and open `http://localhost:5173` while Docker is running for the API.

The enterprise queue/database layout is responsive: stage cards collapse from four columns to two and then one; the repository inspector moves below the records on smaller screens; wide data tables remain horizontally scrollable so OCR evidence is not truncated.

## Real upload flow

```text
1–10 images/PDF pages → original image/PDF storage → Redis case job
→ quality check → conditional enhancement → re-check
→ digital PDF text via PyMuPDF OR hold failures for review (NO OCR), otherwise selected PaddleOCR or Surya 2
→ Surya 2 fallback only for failed/empty Paddle reads
→ AI groups the whole bundle
→ AI proposes fields per group → deterministic evidence/format checks
→ review screen + category approval → searchable fields with source links
```

Pages keep their original upload order. A document can contain nonadjacent pages. Every proposed field carries an exact OCR quotation and source page. Unknown categories are proposed for review; there is no fixed medical document list. Confidence scores are model proposals, not calibrated probabilities.

The case worker quality-checks up to `OCR_BATCH_SIZE` pages as one internal unit. PaddleOCR reads each accepted page through Redis and saves its OCR text and elapsed time immediately, so earlier pages remain visible while later ones run. Pages without usable Paddle results use Surya 2 fallback, including its weak-result full-page recheck. Weak nonempty Paddle reads are flagged for review. Originals are preserved and enhanced inputs are separate PNGs. AI extraction uses the whole case for grouping and document-sized chunks for fields.

For measured OCR-mode latency and a repeatable local accuracy benchmark, see [OCR_TUNING.md](OCR_TUNING.md).

For bundle timing, inspect `ocr_elapsed_seconds` on each page of `GET /cases/{case_id}` or the elapsed column in the ingestion view. This measures the selected OCR route, including any Paddle timeout and Surya fallback; quality-gate timing is reported separately as `quality_gate.elapsed_seconds`. The CPU Surya service can be substantially slower on text-heavy pages. Per-page saving makes finished OCR visible without waiting for the remaining images or AI grouping; it does not itself shorten inference time.

## Model configuration

Docker Compose starts an isolated PaddleOCR CPU worker as the primary and a dedicated Surya 2 llama.cpp service as fallback. The Surya model endpoint is bound only to `localhost:8001` on the host. Both use local model caches. CPU processing of 10 pages may be slow, especially if Paddle times out and Surya fallback runs. Neither engine uses Tesseract or TrOCR. The OpenCV quality gate runs before either OCR engine. See the [PaddleOCR documentation](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html) and [Surya 2 documentation](https://github.com/datalab-to/surya) for model details.

Check model readiness with `Invoke-RestMethod http://localhost:8000/pipeline/status`; it reports Paddle worker and Surya fallback reachability. To exercise the current pipeline, upload through the UI. `scripts.smoke_ocr` remains a Surya-only diagnostic, not a test of the primary path.

For a non-private smoke test, create a fictional prescription image with `python -m scripts.make_smoke_image`, then run `docker compose exec worker python -m scripts.smoke_ocr /app/work/surya-smoke.png`. It includes the dosage notation `0-1-1`; the image is saved under the ignored `work/` directory.

The grouping and field suggestion stages use an OpenAI-compatible chat completions endpoint that returns JSON. Set:

```dotenv
AI_BASE_URL=http://host.docker.internal:11434/v1
AI_MODEL=your-json-capable-model
AI_API_KEY=
```

The example URL works only if an appropriate AI service is running on the host. No document-analysis model or credentials are bundled. If the AI service is absent, the case displays `awaiting_ai_configuration` after OCR; the saved case can be retried with `POST /cases/{case_id}/retry`.

The API image is small. The case worker and Paddle worker have separate dependencies; their first builds may take a while.

## API

### Local PDF Data Extraction

The ingestion workspace also includes a PDF Data Extraction face. It is a
separate React flow and does not alter the image OCR case pipeline. It accepts
PDFs only, detects each file as `text`, `scanned`, or `mixed` with PyMuPDF, and
stores extraction jobs independently. Text-layer pages use deterministic
PyMuPDF rules (key/value, below-label, table, and typed regex strategies). A
page with too little text is rendered and sent through the existing local
PaddleOCR → Surya fallback path. No LLM or external API is used.

The flow is exposed through `POST /api/extract/resolve`, `POST
/api/extract/jobs`, `POST /api/extract/jobs/{id}/files`, `POST
/api/extract/jobs/{id}/run`, `GET /api/extract/jobs/{id}`, and `GET
/api/extract/jobs/{id}/export` (zip) or `/export.csv` (one CSV row per PDF; the UI's
**Download CSV** button). Extraction follows the `extractor.ipynb` logic: ISO dates, numeric
amounts, an `Amounts check` column (bill − non-pay − deductions = paid), and a flagged letter-date
fallback for Claim date. Field labels live in
`app/extract_dictionary.json`; unknown fields can be mapped and saved from the
UI. Jobs are processed on the existing Redis-backed worker and report per-file
data, evidence, typed values, confidence by field, and warnings.

- `POST /cases`: upload 1–10 images or PDF files under repeated `files` fields. PDFs are expanded to a maximum of 10 pages; selectable pages use `pymupdf`, while scanned pages use the normal OCR route.
- `POST /cases/{case_id}/cancel`: stop a queued or active run. Finished pages remain stored; unfinished pages become `cancelled`. Paddle waits poll cancellation and an active Surya HTTP request is closed.
- `POST /cases/{case_id}/retry`: clear cancellation and requeue only unfinished pages while preserving completed OCR results.
- `POST /cases` accepts `ocr_engine` as `paddle` (default) or `surya`, selected in the frontend before Trigger Extraction.
- `GET /cases?limit=50&offset=0` returns a newest-first, read-only repository index for the queue and database pages (maximum 100 bundles per request).
- `GET /cases/{case_id}`: processing status, original pages, proposed documents, field evidence.
- `GET /cases/{case_id}/fields?category=...&field_name=...`: retrieve selected values and source links.
- `GET /cases/{case_id}/pages/{page}/original`: retrieve the uploaded image.
- `GET /cases/{case_id}/pages/{page}/quality-image`: retrieve an approved derived OCR image, when present. The case response includes each page's `quality_gate` report.
- `POST /cases/{case_id}/pages/{page}/compare/paddle`: queue an opt-in Paddle comparison for an earlier Surya case or Surya-fallback page after OCR is done.
- `POST /cases/{case_id}/pages/{page}/compare/verdict`: record a human comparison choice (`{"verdict":"surya"}`, `paddle`, `tie`, or `neither`).
- `POST /cases/{case_id}/documents/{document_id}/approve`: approve a proposed category when its fields pass evidence checks.
- `PATCH /cases/{case_id}/documents/{document_id}/category`: label a document proposed as unknown, then review its category.
- `POST /cases/{case_id}/retry`: resume a saved case after configuring the model service.

For a real deployment, add user authentication, access controls, encrypted object storage, database migrations, and durable queue recovery before processing private customer records.

## Pre-OCR image quality gate

`app/services/quality_gate.py` is a standalone rule-based OpenCV module. Good images go straight through. Failed metrics trigger only their enabled fix, in order **denoise → deskew → CLAHE → conservative upscale → sharpen**, followed by a fresh check (one attempt by default). The resulting original or derivative then goes automatically to the selected OCR engine. If the re-check still fails, OCR continues with a visible quality warning and the result remains `needs_review`; files that cannot be decoded or normalized still stop before OCR. Upscaling can improve OCR input size but cannot recreate missing source detail. TrOCR code, overrides, and direct dependencies have been removed; historical case results are not rewritten.

These defaults are **uncalibrated starting points**, not pharmaceutical validation or OCR-accuracy guarantees:

| Environment variable | Default | Pass condition |
| --- | ---: | --- |
| `QUALITY_BLUR_MIN` | 100 | Native grayscale Laplacian variance ≥ cutoff |
| `QUALITY_CONTRAST_MIN` | 20 | Native grayscale standard deviation ≥ cutoff |
| `QUALITY_SKEW_MAX` | 3 | Absolute Hough-estimated text-line angle ≤ cutoff (degrees) |
| `QUALITY_NOISE_MAX` | 8 | Mean grayscale denoising residual ≤ cutoff (sample max side 640) |
| `QUALITY_TEXT_HEIGHT_MIN` | 12 | Estimated lower-quartile text-component height ≥ cutoff (source pixels) |
| `QUALITY_MIN_SIDE` | 200 | Shortest image dimension ≥ cutoff |
| `QUALITY_MAX_ATTEMPTS` | 1 | Conditional enhancement/recheck attempts; 0 holds failing originals |
| `QUALITY_UPSCALE_MAX_FACTOR` | 4 | Maximum interpolation scale used for OCR input |
| `QUALITY_UPSCALE_MAX_SIDE` | 3000 | Maximum long side of an upscaled OCR derivative |
| `QUALITY_UPSCALE_MARGIN` | 1.35 | Safety margin for non-linear text-height estimates after interpolation |

Add overrides to `.env`, then recreate `worker` and `paddle-worker` using Docker Compose. `QualityConfig.from_env()` also accepts `QUALITY_MIN_TEXT_COMPONENTS`, `QUALITY_SCORE_MAX_SIDE`, `QUALITY_NOISE_SAMPLE_SIDE`, `QUALITY_DENOISE_H`, `QUALITY_CLAHE_CLIP`, `QUALITY_CLAHE_TILE`, `QUALITY_SHARPEN_AMOUNT`, `QUALITY_MAX_SKEW_CORRECTION`, and `QUALITY_CALIBRATION_ID`. For ablation, disable one fix using `QUALITY_DENOISE_ENABLED=false`, `QUALITY_DESKEW_ENABLED=false`, `QUALITY_CONTRAST_ENABLED=false`, `QUALITY_UPSCALE_ENABLED=false`, or `QUALITY_SHARPEN_ENABLED=false`. This never bypasses the final check.

For calibration, collect 30–50 de-identified labels with human good/bad judgements and verified text, including representative handwriting, blur, skew, noise, and lighting. Create a CSV with `id,path,quality_label` (`good`/`bad`); paths are relative to the CSV folder and IDs must be non-sensitive. Score without loading OCR or changing source images:

```powershell
docker compose exec -T worker python -m scripts.calibrate_quality work/labels.csv
# Compare with no enhancements, or omit one operation:
docker compose exec -T worker python -m scripts.calibrate_quality work/labels.csv --disable-all-enhancements
docker compose exec -T worker python -m scripts.calibrate_quality work/labels.csv --disable sharpen
```

The command emits JSONL scores, decisions, labels, configuration, and gate latency. Plot each metric's good/bad distributions, select thresholds on a calibration split, then report false accept/reject rates on a separate held-out split. Use the same thresholds/dataset for each ablation. To measure OCR CER/WER, run `scripts.benchmark_ocr` with and without `--quality-gate` against verified transcriptions. Raw mode is an explicit offline benchmark only, not a live-pipeline bypass. Report held-page coverage as well as accuracy on accepted pages, warm/cold latency, median and p95. No accuracy improvement is claimed until measured.

Each page stores original/post scores, individual checks, failure reasons, operations/attempts, configuration/version, source/input hashes, derived input reference and gate latency in case analysis; retries append `quality_history`. Worker logs add bundle/image IDs. The React inspection panel displays scores and the actual approved derivative. Original files are never overwritten.

Limitations: global contrast depends on whitespace; noise can inflate Laplacian sharpness; barcodes, signatures and graphics can confuse component/Hough estimates. Unknown skew/text size fails closed. Large rotations, perspective distortion and multiple-frame TIFFs require separate handling (split TIFF pages first). Sharpening is not recovery of truly blurred characters. Scoring/enhancement latency depends on image size and hardware; it is not guaranteed to take milliseconds. See OpenCV's [CLAHE](https://docs.opencv.org/4.x/d5/daf/tutorial_py_histogram_equalization.html), [Hough transform](https://docs.opencv.org/4.x/d9/db0/tutorial_hough_lines.html), and [denoising](https://docs.opencv.org/4.x/d5/d69/tutorial_py_non_local_means.html) documentation.

Run unit tests inside the built worker with `python -m unittest discover -s tests` (API tests also require `httpx`, listed in `requirements-test.txt`). These exercise synthetic degradations and blocked-OCR routing; they are not a substitute for a labelled real-image benchmark.
# AI classification progress and navigation

OCR completion and AI classification are separate stages. Categories are saved as soon as grouping succeeds, before field extraction. A field extraction failure retains the category and marks the result for review. Empty or invalid AI JSON is retried once; failures appear in the ingestion view and the latest database record.

OpenRouter requests use JSON mode with a bounded output budget and disabled reasoning where supported. Free-model availability and response times vary; configuration alone does not prove that an AI request succeeded.

Workspace navigation keeps the ingestion page mounted, including its selected files and active bundle. The most recently viewed bundle is restored after reopening the app, and the repository refreshes every three seconds while open.
