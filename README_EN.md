<div align="center">

# 📖 YIMU Lit (一幕·YIMU 神笔文学创作台)

**A nine-genre full-cycle AI writing workbench for novelists, screenwriters and literary creators — from a one-line premise to a finished, exportable book**

[![Version](https://img.shields.io/badge/version-4.23.1-orange.svg?style=flat-square)](CHANGELOG.md)
[![Release](https://img.shields.io/badge/release-v4.23.1-blueviolet.svg?style=flat-square)](https://github.com/yimuhub/yimu-lit/releases)
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
- **Body-text purity domain (P0 iron rule)**: copying and exporting yield only "chapter title + chapter-opening intro + body". Chapter summary, chapter notes, revision explanations, review scores and word-count stats never appear in any copy, preview, manuscript file or export format.
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
- **Professional export**: Markdown / Word / EPUB / print layout / Fountain / FDX for screenplays, with automatic pen-name signatures. All exports (single chapter, whole book, preview, clipboard) go through the body-text purity domain — process metadata is stripped before it reaches you.
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
  purity.py            Body-text purity domain (single source of truth for copy / preview / manuscript / export)
  skillkit.py          Skill library loading & stage injection (9 genres × stages)
  llm.py               Multi-provider gateway (caching / throttling / budget / wall-clock guard)
static/v2/             Frontend (ES modules, no build step)
tests/                 480+ regression cases
```

---

## 🧼 Body-text purity domain (engineering iron rule, non-negotiable)

**Body text = chapter title + chapter-opening intro + body content.** Everything else —
`## Chapter summary`, `## Chapter notes`, `## Revision notes`, `## Review scores`, word counts,
reviewer comments, model thinking tags — is process metadata and **must never appear in the
clipboard, the preview, the manuscript file, or any export format**.

- Single implementation: `bot/purity.py` on the backend, `static/v2/js/core/purity.js` on the frontend (line-by-line identical behaviour).
- **Body contract markers (v4.22.3)**: generation is instructed to wrap the prose between
  `=== 一幕·YIMU 神笔工作台·开篇 ===` and `=== 一幕·YIMU 神笔工作台·结尾 ===`; everything outside
  is structurally not body text, so no vocabulary list has to guess.
- Single entry points: `chapter_text()` (copy / display), `chapter_body()` (export / manuscript), `sanitize_chapter()` (before writing to disk).
- Guard test: `python -m pytest tests/test_v422_purity.py tests/test_v4222_resume_plan.py -q`.
- Healing old drafts: `python tools/purify_data.py` (read-only check) → `--apply` writes back with automatic backups.
- Any new export format or copy entry point must go through these functions. **Blacklist-style cleaning is forbidden** — miss one section name and a whole class of contamination leaks through.
- Developer details in the "Iron Rule 0 · Body-Text Purity Domain" section of [`功能表.md`](功能表.md).

### Resume-from-checkpoint & long-form coherence (v4.22.3 → v4.23.0)

- **"Continue" means finish what was interrupted**: `bot/journal.py` records the active task
  (writing / rewrite / scoring / chapter-card planning), progress, current chapter and interruption
  reason in `<project>/任务档案.json`, plus a per-chapter draft checkpoint in `.draft/`. Resuming
  restores the task first, then continues from the draft tail — never from the first paragraph again.
- **Any long task resumes (v4.23.0)**: `bot/tasks.py` is a registry — decorate a function with
  `@runs("kind")` and that task class gains interrupt-and-resume; `tasks.resume()` is the single entry
  point behind "continue". An unrecognised task type is reported honestly instead of silently
  falling back to writing the next chapter — picking the wrong task is worse than forgetting it.
- **Progress is booked per chapter number**: parallel rewrite runs one thread per chapter and they
  finish out of order, so the journal stores *which* chapters are done, not *how many*. Otherwise
  resuming skips unfinished chapters and redoes finished ones.
- **A failed chapter is not "done"**: it stays in the queue for the next "continue"; only after three
  consecutive failures is it skipped (usually a corrupt file, not the model) so a book can still finish.
- **Zombie tasks are revived**: when the process is killed mid-run it never gets to write "paused",
  so the journal stays at `running`. Startup reconciliation (`journal.reconcile_all`) turns those into
  resumable tasks; without it "continue" believes the task is still alive and refuses to take over.
- **No outline, no writing**: when rolling planning gets an empty LLM response it used to emit
  placeholder chapter cards ("continue this arc…") with no goal or conflict, i.e. writing without
  an outline — the reason a 100-chapter book drifted into sci-fi by chapter 90. Every chapter now
  passes `_ensure_card_planned`: a placeholder card is re-planned on the spot, and if that fails the
  chapter is skipped and the run pauses instead of drifting silently.

### Commits & CI (v4.23.0)

- Commit straight after each change: `./tools/commit.sh "feat: v4.23.0 one-line summary"`.
  The message must carry the `v<version>` and CHANGELOG must have an entry for it, otherwise the
  script refuses — preventing "code shipped, version number left behind".
- **CI is not wired to push**: `.github/workflows/ci.yml` is manual-dispatch only. Checks run locally
  (`bash tools/static_check.sh` + `python -m pytest -q`), in seconds and without queueing.
- Cost: this is a public repository, so standard-runner Actions minutes are **free and unlimited**.
  Going manual was about signal, not money — with push triggers the lint step failed first and the
  tests never even ran, so every commit got a red X within 20 seconds.

---

## 📜 License & Copyright

This project is licensed under **BUSL-1.1 (Business Source License 1.1)**, copyright **yimu.hyipf.com**.

- Personal learning, research, non-commercial use, and personal content creation (including revenue from your own accounts): **free of charge, no application required**
- Free for any use whose direct commercial value stays below RMB 100,000 in any rolling 12-month period
- Reaching the threshold requires a commercial license (free upon registration, contact yimu@hyipf.com)
- Each version converts to Apache License 2.0 four years after its first public release

See [LICENSE](LICENSE). Licensor: [yimu.hyipf.com](https://yimu.hyipf.com)
