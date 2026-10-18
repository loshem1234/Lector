# Lector

A private read-along web app for your custom Gemini 3.8 TTS voices. Paste text or upload a PDF, and it reads aloud in your voice while highlighting each word. Download the result as an MP3 with chapter markers.

## What it does

- **Input.** Paste text, or upload a PDF. Scanned pages are read with OCR (English, Ancient and Modern Greek, Latin, German, French, Italian).
- **The book's own structure.** Sections come from the PDF's outline (bookmarks). If there is none, they come from its headings. In pasted text, lines like `Chapter 3`, `Part II` or `# Heading` start sections.
- **Apparatus.** Running heads, page numbers, footnotes and footnote markers are shown on screen but not read aloud.
- **Voices.** Your custom voices (designed or replicated) are listed from your Gemini project. You can add an optional delivery note such as "calm, lecture pace".
- **Live highlighting.** Each passage is synthesized with `gemini-3.8-flash-tts`, then transcribed back with `gemini-3.5-transcribe` to get word timings. Those timings are aligned to the source words, accents included, so Greek still lines up.
- **Two views.** A clean reflowed text view, or the original PDF page with the highlight drawn on it.
- **Controls.** Tap any word to start there. Skip 15 seconds back or forward, change speed, and use the lock-screen controls on your phone. Your place is saved on the server, so it carries between computer and phone.
- **Streaming generation.** Audio is produced passage by passage, starting where you are reading, so playback begins within seconds.
- **Library.** Every document is kept until you delete it. You can delete just its audio to save space and keep the text.
- **Export.** One MP3 with chapter markers, a zip with one MP3 per section, or a single section's MP3 from Contents.

## Deploy on Railway

1. Put this folder in a GitHub repository, for example `lector`.
2. In Railway, choose **New Project → Deploy from GitHub repo** and pick the repository. Railway builds it with the included `Dockerfile`.
3. Add a **Volume** to the service, mounted at `/data`. This is where the library lives.
4. Under **Variables**, set:

   | Variable | Value |
   |---|---|
   | `GEMINI_API_KEY` | Your Gemini API key. It must come from the same Google project where you created your voices. |
   | `ADMIN_PASSWORD` | The password for the site. |
   | `SESSION_SECRET` | Optional. A long random string; changing it signs everyone out. |

5. Under **Settings → Networking**, choose **Generate Domain**. Open the URL, sign in, and start reading.

On your phone, open the URL and use **Add to Home Screen**. It then opens like an app.

### Optional settings

| Variable | Default | Meaning |
|---|---|---|
| `TTS_MODEL` | `gemini-3.8-flash-tts` | The speech model. |
| `TRANSCRIBE_MODEL` | `gemini-3.5-transcribe` | The model used for word timings. |
| `CHUNK_CHARS` | `1100` | Characters per passage, roughly 60–90 seconds of speech. |
| `CONCURRENCY` | `2` | Passages generated at the same time. Raise it if your rate limits allow. |
| `OCR_LANGS` | `eng+grc+ell+lat` | Tesseract languages used for scanned pages. |
| `MP3_BITRATE` | `96k` | Bitrate for the stored audio. |

## Run it on your own computer

```bash
pip install -r requirements.txt   # also needs ffmpeg and tesseract installed
export GEMINI_API_KEY=...  ADMIN_PASSWORD=...
uvicorn app.main:app --port 8000
```

Without `GEMINI_API_KEY`, the app runs in mock mode and plays tones in place of speech. This is useful for trying the interface without spending credits.

## Notes

- **Ancient Greek** is spoken with modern Greek pronunciation. Highlighting in Greek passages may move in slightly larger steps.
- **Changing the voice** of a document regenerates its audio.
- **Cost.** Each passage makes one TTS call and one transcription call.
- **Data layout.** Everything is stored in `/data/docs/<id>/`: the source PDF, `doc.json` (the parsed text and layout), and `audio/` (MP3s plus word timings). This makes it straightforward to move the library into the Archivum later.

## Layout

```
app/
  main.py     web routes, sign-in, library
  ingest.py   PDF and text parsing: sections, apparatus, OCR
  gemini.py   voices, speech and transcription calls (REST)
  align.py    matching transcript timings to the source words
  jobs.py     passage planning and background generation
  export.py   MP3 with chapters, per-section MP3s, zip
static/       the web page (no build step)
```
