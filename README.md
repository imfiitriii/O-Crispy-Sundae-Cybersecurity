# O-Crispy Sundae - ScrollSafe

ScrollSafe warns users of any deepfakes, AI-generated media or false information in user For You Page (FYP) a allow users to upload media and quickly verify whether it contains any deepfakes or AI-generated content.

Built for Codefest by SYNTECHUTP, Track 4: Cybersecurity.

## Two core features :

1. **An AI helper for TikTok and Instagram Reels FYP.** While you scroll, a small badge on the video warns you if it may be a deepfake, made by AI, a scam, or spreading false information, in plain words that anyone can understand.

2. **Deepfake, AI and Content Verification Platform** An app where users can go to upload any picture, video or link with caption to check for deepfake, AI-generated content or false information.

For both feature, every content evaluated will be given a verdict as below :

| Badge | Meaning |
|---|---|
| 🟢 Looks OK | No warning signs found |
| 🟡 Be careful | Some warning signs (likely AI-made, labelled as AI, risky claims, scam words, community reports) |
| 🔴 Warning | Strong signs of a deepfake, scam, or a claim fact-checkers rated false |

Tap **Why?** for the reasons and the deepfake / AI scores, **🔊 Read** to hear it aloud, and **🚩 Report** to warn other users.

## How it works

1. The Chrome extension finds the video you're watching. After you stay on it for 2.5 seconds, it reads the video ID, caption, creator name and any AI label (TikTok's "Contains AI-generated media", Instagram's "AI info", or hashtags like `#ai`), and grabs 5 frames.
2. It asks the backend if anyone has already checked this video. If yes, the badge shows instantly.
3. If not, the backend runs three checks at the same time:
   - **Deepfake:** finds faces (OpenCV) and runs a dedicated face-deepfake classifier ([`prithivMLmods/Deep-Fake-Detector-v2-Model`](https://huggingface.co/prithivMLmods/Deep-Fake-Detector-v2-Model)). Gemini also checks that the face stays consistent across frames. The two are weighted equally.
   - **AI-generated:** runs an AI-vs-human image classifier ([`Ateeqq/ai-vs-human-image-detector`](https://huggingface.co/Ateeqq/ai-vs-human-image-detector)) on the frames, weighted equally with Gemini's check for visible AI mistakes. A platform or creator AI label counts as AI-made.
   - **False information and scams:** Gemini pulls out factual claims and scam signs. The claims are searched in the **Google Fact Check Tools API** (English and Malay), and the caption is matched against a scam keyword list based on public MCMC, PDRM and Bank Negara warnings.
4. The scores are combined into green, yellow or red with a one-line reason, saved in the shared database, and sent back to the badge.

```
Chrome extension ──► FastAPI backend ──► cache lookup (video ID, then frame hash)
  (frames, caption,        │
   AI labels)              ├─► face crops ─► deepfake classifier (Hugging Face)
                           ├─► frames ─────► AI-image classifier (Hugging Face)
                           └─► Gemini (main model, backup if busy) ─► claims ─► Google Fact Check API
                                                                  └► scam signs + scam keyword list
                           ▼
                  combine ─► 🟢 / 🟡 / 🔴 + reason ─► SQLite (shared verdicts + reports)
```

**How the verdict is decided**
- The open-source classifiers often misjudge real, compressed phone videos, so on their own they can only give **yellow**. **Red** needs a second, independent signal: Gemini seeing the same problem, a fact-check rating the claim false, or scam words.
- Without Gemini, the local models need to be much surer (90% instead of 60%) to give yellow.
- Unverified claims only matter when Gemini also rates the misinformation risk as medium or high, so ordinary news clips stay green.

**The AI helper (Gemini)**
- One call per video with 3 of the frames (first, middle, last) and "low thinking" to keep it fast.
- Each model gets 12 seconds. If the main model (`gemini-3.8-flash`) is busy, rate-limited, slow or erroring (429, 500, 502, 503, 504), the backup model (`gemini-3.5-flash-lite`) is tried once.
- Results checked without Gemini aren't saved, so the video gets a full check next time.
- It can be switched off in the extension for faster, local-only checks.

**Security and privacy choices**
- Only the backend writes verdicts. Community reports are stored separately and can only *raise* a warning (3+ reports → at least yellow), never lower it, so scammers can't mark their own videos as safe.
- Each user is an anonymous random ID, stored hashed. No browsing history is kept: only video IDs and verdicts.
- Re-uploaded copies of a known video are matched by perceptual frame hashes, so they reuse the verdict.
- If no checks could run, the badge says "Couldn't check" instead of pretending the video is safe.

## Setup

### 1. Backend

Needs Python 3.10 to 3.12 (recommended; PyTorch may not have wheels for the newest Python yet).

Windows:

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS/Linux: same, but use `source .venv/bin/activate` and `cp .env.example .env`.

Fill in `.env`:
- `GEMINI_API_KEY`: free from [Google AI Studio](https://aistudio.google.com/apikey).
- `FACTCHECK_API_KEY`: in [Google Cloud console](https://console.cloud.google.com/), enable **Fact Check Tools API**, then create an API key under *APIs & Services → Credentials*.

Start it:

```bash
uvicorn app.main:app --reload --port 8000
```

The first start downloads the two Hugging Face models (a few hundred MB), so do it once before the demo. Open http://localhost:8000/health to see what loaded. Each model line shows its labels; if a model's labels aren't recognised as real/fake, it's ignored and the line says so.

Everything is optional: without keys or models, the remaining checks still run.

### 2. Chrome extension

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and choose the `extension` folder.
3. Open https://www.tiktok.com and scroll. The badge appears on the video after about 2.5 seconds.
4. Click the ScrollSafe icon to:
   - turn ScrollSafe on or off
   - use large text or read warnings aloud
   - switch the **AI helper (Gemini)** on or off (on is more accurate, off is faster)
   - choose the language: **English** (default) or **Bahasa Melayu**
   - change the server address (for example an ngrok URL)

After changing extension files, click the reload arrow on ScrollSafe in `chrome://extensions` and refresh the TikTok tab.

### 3. Website: check a link or a file (optional)

With the backend running, open http://localhost:8000. **Paste a link** to a TikTok or Instagram Reels video, or **upload** a photo or video, and it is checked with the same pipeline. Use the **EN / BM** switch at the top right to change the language.

TikTok links are checked from the full video (5 frames, like the extension). Instagram usually hides its pages from visitors who aren't logged in; when that happens the site asks you to upload a screen recording instead.

### 4. Tests

With the venv active:

```bash
cd backend
pip install pytest
python -m pytest -q
```

The tests run without API keys or models.

## Configuration

All settings live in `backend/.env` (see `backend/.env.example`). Restart the backend after changing them.

| Setting | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | (empty) | Turns on the AI helper |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Main Gemini model |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | Backup when the main model is busy or slow; empty disables it |
| `GEMINI_TIMEOUT_S` | `12` | Seconds to wait for one model before trying the backup |
| `FACTCHECK_API_KEY` | (empty) | Turns on Google Fact Check search |
| `DEEPFAKE_MODEL` | `prithivMLmods/Deep-Fake-Detector-v2-Model` | Hugging Face deepfake classifier |
| `AI_IMAGE_MODEL` | `Ateeqq/ai-vs-human-image-detector` | Hugging Face AI-image classifier |
| `LOAD_LOCAL_MODELS` | `1` | `0` skips the local models (faster start, fewer signals) |
| `DB_PATH` | `scrollsafe.db` | SQLite file for verdicts and reports |
| `RED_AT` | `0.75` | Deepfake score for red (Gemini must also see it) |
| `DEEPFAKE_YELLOW_AT` | `0.6` | Deepfake score for yellow |
| `AI_YELLOW_AT` | `0.6` | AI-generated score for yellow |
| `AI_WITH_RISK_RED_AT` | `0.6` | AI-generated score for red when the video also makes risky claims |
| `LOCAL_ONLY_YELLOW_AT` | `0.9` | Yellow bar when Gemini is unavailable or turned off |

If too many normal videos get yellow, raise the yellow thresholds. Tap **Why?** on a badge to see the scores.

## Troubleshooting

| What you see | What it means / what to do |
|---|---|
| "AI helper off" under **Why?** | Gemini didn't answer (or the switch is off). Look in the backend window for `Gemini call failed:` |
| `404 NOT_FOUND` for a model | Google retired that model name. Change `GEMINI_MODEL` or `GEMINI_FALLBACK_MODEL` in `.env` |
| `503` / `504` / "high demand" | Google is overloaded. The backup model takes over automatically; if it keeps happening, set `GEMINI_MODEL=gemini-3.5-flash-lite` or switch the AI helper off |
| "Couldn't check" | The backend isn't reachable. Check it's running and the server address in the popup |
| Badge doesn't change after editing the extension | Reload ScrollSafe in `chrome://extensions` and refresh the page |

## Sharing the backend (optional)

To use the extension on another laptop, run `ngrok http 8000` and paste the `https://….ngrok-free.app` address into the extension's popup.

## Project layout

```
backend/
  app/
    main.py            API: /analyze, /verdict, /report, /check-upload, /check-link, /health
    links.py           Reads pasted TikTok / Instagram links (video ID, caption, cover, video)
    config.py          Settings from .env (models, timeouts, thresholds)
    detectors.py       Hugging Face deepfake + AI-image classifiers
    gemini_client.py   One Gemini call per video, with backup model
    factcheck.py       Google Fact Check Tools API
    scam_rules.py      Scam keyword check (scam_keywords.json)
    scoring.py         Combines signals into green / yellow / red
    db.py              SQLite verdict cache and reports
    imaging.py         Frame decoding, face crops, perceptual hashes
  tests/
extension/             Chrome extension (Manifest V3)
web/                   Upload page
```

## Limits

- ScrollSafe gives **warning signs, not proof**, which is why it combines several independent checks and community reports.
- The open-source classifiers are community models; their accuracy figures are self-reported and they can miss new deepfake tools. The thresholds (see [Configuration](#configuration)) should be tuned on your own test videos.
- Frames don't capture audio, so voice-clone deepfakes aren't detected yet.
- TikTok and Instagram change their pages often; the extension finds videos by the `<video>` tag and links rather than class names to reduce breakage.
- Gemini model names change over time; if one is retired, update `GEMINI_MODEL` in `.env`.

## Roadmap

Mobile app, WhatsApp bot ("forward a video to check it"), audio deepfake detection, a live link to MCMC's scam data, and Malay-language badges.
