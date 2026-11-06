# Pick a local voice engine

Find the local TTS engine claude-speak will use. Nothing in this repo changes.

## Setup
- One folder per engine under `~/code/AI/audio/<engine>/`, each with its own venv. Reuse a folder that already exists.
- Candidates: Kokoro and Piper, plus any other engine worth a listen.
- One script per engine speaks the same three chunks: a short sentence, a typical ~300-character paragraph, and one with code names like `HoleCarver.SampleWeight`.
- CPU only.

## Test each engine
I test every engine myself and fill its row:

| Engine | Time to first audio (typical chunk) | Word timings | Install | Sound |
|---|---|---|---|---|

- **Time to first audio:** from calling the engine to audio being ready, model already loaded. A successful edge-tts request takes about 1 s.
- **Word timings:** per word, per phoneme (needs mapping to words), or none (read-along highlights the whole chunk).
- **Install:** pip size, model size and whether it downloads itself, system packages.

## Pick
1. Drop engines clearly slower than edge-tts to first audio.
2. Of the rest, the one that sounds best wins.
3. Word timings, then a smaller install, break ties.
