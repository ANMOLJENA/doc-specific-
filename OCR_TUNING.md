# Surya 2 OCR tuning log

This records the earlier Surya-primary experiments. New uploads now use PaddleOCR first; Surya 2 is the fallback when Paddle errors, times out, or returns no text.

This prototype uses `datalab-to/surya-ocr-2` via llama.cpp on CPU. The OCR worker first runs layout-guided recognition and uses a full-page second pass only when a page is weak. Uploaded originals are never overwritten. The new pre-OCR quality gate may create a separate enhanced input; the historical measurements below predate that gate.

## Baseline (fictional image, not a medical accuracy benchmark)

The image `work/surya-smoke.png` and its deliberately blurred copy were compared with `tests/fixtures/surya-smoke.txt`. Both modes matched that simple reference exactly after case/punctuation/whitespace normalization. These two images are far too small a dataset to claim general accuracy.

| Image | Mode | Seconds | Normalized CER | Normalized WER |
| --- | --- | ---: | ---: | ---: |
| Clean fictional prescription | Layout-guided | 25.36 | 0.000 | 0.000 |
| Clean fictional prescription | Full-page | 50.74 | 0.000 | 0.000 |
| Blurred fictional prescription | Layout-guided | 56.64 | 0.000 | 0.000 |
| Blurred fictional prescription | Full-page | 49.89 | 0.000 | 0.000 |

Seven stored one-image cases with recorded timestamps took 35.3–155.6 seconds each (different source images, so not a controlled benchmark). Only one of 15 recorded pages triggered the full-page recheck. The largest obvious latency opportunity is the currently CPU-only model service, not the recheck threshold. The host has a GTX 1660 Ti with 6 GB VRAM, but GPU inference has not been tested in Docker and the production configuration remains unchanged.

At the time of this benchmark, Windows reported only about 0.5 GB of free system RAM. A second full model server was therefore not launched alongside the live server; doing so could destabilize OCR or other applications.

## Repeat a test

With Docker running:

```powershell
docker compose exec -T worker python -m scripts.benchmark_ocr work/surya-smoke.png --expected tests/fixtures/surya-smoke.txt --modes layout full_page
```

For a new local image, prepare a UTF-8 transcription and pass it with `--expected`. The script prints timing, a model score, normalized character error rate (CER), and normalized word error rate (WER), but not the image's text. Keep sensitive images and transcriptions out of source control.

## Next experiments

1. Measure 20–50 representative, de-identified images with verified transcriptions, including printed, handwritten, tilted, and blurry pages. Report CER/WER by type as well as median and p95 latency.
2. If approved, benchmark the same model in a separate CUDA container before switching the live service. Compare quality and warm latency on the same images; keep CPU as rollback.
3. Only consider weight fine-tuning after the labeled evaluation set shows repeatable errors. Training would need the trainable model weights and a separate training workflow; the GGUF inference file is not itself a ready-made training setup.

## Quality-gate ablation

See the README quality-gate section for scoring a labelled CSV, tuning provisional thresholds, and disabling individual operations. For the same image/reference, compare this explicit raw baseline with a gated run:

```powershell
docker compose exec -T worker python -m scripts.benchmark_ocr work/surya-smoke.png --expected tests/fixtures/surya-smoke.txt --modes layout
docker compose exec -T worker python -m scripts.benchmark_ocr work/surya-smoke.png --expected tests/fixtures/surya-smoke.txt --modes layout --quality-gate
```

The gated variant checks quality before loading the OCR engine and stops on rejected inputs. Track rejection/coverage alongside CER/WER and gate + inference latency. Hold all other conditions fixed for each operation ablation. Synthetic unit-test passes establish routing behaviour only; no real-data calibration or accuracy gain has been measured for this phase.
