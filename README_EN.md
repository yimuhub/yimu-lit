<div align="center">

# 📖 YIMU Lit (一幕·YIMU 神笔文学创作台)

**A nine-genre full-cycle AI writing workbench for novelists, screenwriters and literary creators — from a one-line premise to a finished, exportable book**

[![Version](https://img.shields.io/badge/version-4.15.12-orange.svg?style=flat-square)](CHANGELOG.md)
[![Release](https://img.shields.io/badge/release-v4.15.12-blueviolet.svg?style=flat-square)](https://github.com/yimuhub/yimu-lit/releases)
[![License: BSL 1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg?style=flat-square)](LICENSE)
[![Licensor](https://img.shields.io/badge/Licensor-yimu.hyipf.com-0284c7.svg?style=flat-square)](https://yimu.hyipf.com)
[![Copyright](https://img.shields.io/badge/Copyright-%C2%A9%202026%20yimu.hyipf.com-orange.svg?style=flat-square)](https://yimu.hyipf.com)
[![Python](https://img.shields.io/badge/python-3.9%2B-green.svg?style=flat-square)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg?style=flat-square)](https://fastapi.tiangolo.com)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows%20%7C%20Docker-blueviolet.svg?style=flat-square)](#-quick-start)

<p align="center">
  <b>One Thought. One Scene. Infinite Worlds.</b><br>
  <i>一念起 · 一幕开 · 万象生</i>
</p>

[简体中文](README.md) • **English**

[✨ Features](#-features) • [🚀 Quick Start](#-quick-start) • [🏛️ Architecture](#-architecture) • [📜 License](#-license--copyright)

</div>

---

## 💡 What is this?

**YIMU Lit** is a pure-text AI literary creation platform: chat to start writing, with a full pipeline from a one-line premise to a finished book. It covers **nine genres** — novel, screenplay, poetry, prose, thesis, children's literature, press release, copywriting, and other content (murder-mystery scripts / podcasts / courses / speeches / book digests / film reviews).

Key differentiators:
- **Million-word long-form engine**: four-layer planning (master plan → volume → arc → chapter) supporting 100–1500 chapters; rolling planning with re-planning proposals; every chapter is written against context compiles of summaries, active foreshadowing, character states and settings, plus a consistency covenant that keeps characters, events and worldview coherent across the whole book.
- **Batch writing under your control**: chapter-by-chapter or scene-by-scene, batches of 5/10/20/50, or write everything at once — simply say "write 10 more chapters" or "finish it all" in the chat.
- **Multi-agent parallel writing**: several writing roles work on different chapters simultaneously, aligned against a global blueprint with continuity bookkeeping and automatic restoration of low-scoring chapters.
- **Anti-AI-flavor system**: three-layer slop scanning, a craft ledger that prevents repetitive hooks, and a trigram similarity gate against near-duplicate chapters.
- **Pen-name system**: manage multiple pen names, set per-genre defaults, or define one on the fly in the creation wizard — signatures flow automatically into the book and Word/EPUB/Markdown exports.

---

## ✨ Features

### 📚 Nine genres, full pipeline
Every genre ships its own engine: dedicated wizard, writing roles and skills, workbench tabs, and plan/state views.

| Genre | Highlights |
|---|---|
| Novel | Million-word layered planning, foreshadow ledger, timeline, story bible, craft ledger |
| Screenplay | Scene list, era-consistency red line, character-presence audit, storyboard generation (incl. AI short-drama) |
| Poetry | Metrical validation for regulated verse & ci patterns, imagery density, word-crafting advice |
| Prose | Three-layer AI-slop scanning (expression / craft / story logic), structure & detail density |
| Thesis | Outline, chapter-by-chapter writing, citation pool, AIGC-humanizing pass |
| Children's lit | Age-graded language, values red lines, parent-child interaction, illustration prompts |
| Press release | Inverted pyramid, 5W1H fact checking, multi-version output |
| Copywriting | Hook library (10 candidates), platform adaptation, AIDA/PAS frameworks |
| Other | Murder-mystery script / podcast / course / speech / book digest / film review templates |

### 🎛️ Creation workbench
- **Chat-to-revise**: issue revision commands in the chat; @ a role for group discussion; every edit is archived and traceable.
- **Consistency audit + one-click fix**: scans character/foreshadowing/setting conflicts; fix one or fix all (rewrite-class issues auto-trigger targeted rewrites).
- **Skill evolution**: rate replies → distill a writing profile → generate candidate skills → evaluate and promote, so the system keeps adapting to your style.
- **Reference RAG**: upload txt/md/docx/pdf/epub references up front; retrieved per chapter and injected as fact- or style-oriented context.
- **Professional export**: Markdown / Word / EPUB / print layout / Fountain / FDX for screenplays, with automatic pen-name signatures.
- **Creation budget**: per-book token budget guardrail, with an unlimited mode.

---

## 🚀 Quick Start

```bash
pip install -r requirements.txt
python run.py          # default http://127.0.0.1:46821
```

- macOS / Linux: `./start.sh`; Windows: double-click `start.bat`
- On first run, configure an OpenAI-compatible endpoint in Settings (DeepSeek / Kimi / local Ollama all work)
- `?mock=1` explores the full UI with zero configuration (built-in mock data, no model calls)
- Docker: `docker-compose up -d`

The UI is a DeepSeek-style three-pane workbench (session sidebar + chat + workbench); the legacy interface remains at `/classic`.

---

## 🏛️ Architecture

```
app.py                 FastAPI main app (225 routes)
bot/
  writer.py            Novel engine (million-word layered planning / parallel / quality gates)
  script_writer.py     Screenplay engine (scenes / storyboard / era validation)
  poetry|prose|thesis_writer.py   Poetry / prose / thesis engines
  children|news|copy|other_writer.py  Light-genre engines (mini_writer_base template)
  longform.py          Layered planning (volumes / arcs / chapter cards / rolling re-planning)
  context.py           Per-chapter context compiler (cards / state / foreshadowing / blueprint / covenant)
  continuity.py        Consistency audit and one-click fixing
  skillkit.py          Skill library loading & stage injection (9 genres × stages)
  llm.py               Multi-provider gateway (caching / throttling / budget / wall-clock guard)
static/v2/             Frontend (ES modules, no build step)
tests/                 470 regression cases
```

---

## 📜 License & Copyright

This project is licensed under **BUSL-1.1 (Business Source License 1.1)**, copyright **yimu.hyipf.com**.

- Personal learning, research, non-commercial use, and personal content creation (including revenue from your own accounts): **free of charge, no application required**
- Free for any use whose direct commercial value stays below RMB 100,000 in any rolling 12-month period
- Reaching the threshold requires a commercial license (free upon registration, contact yimu@hyipf.com)
- Each version converts to Apache License 2.0 four years after its first public release

See [LICENSE](LICENSE). Licensor: [yimu.hyipf.com](https://yimu.hyipf.com)
