<div align="center">

<img src="docs/assets/banner.jpg" alt="Text Surgeon — point, don't quote." width="920">

# Text Surgeon v2.3

**Surgical, AI-Assisted Precision Editing & Autonomous Multi-Round Agent Engine for Long Documents.**<br>
Your AI points at a block with ~15 words — a deterministic engine replaces the whole thing, byte-exactly.

[![Version](https://img.shields.io/badge/version-2.3.0-0d9488)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-2dd4bf)](CONTRIBUTING.md)
[![Tests](https://img.shields.io/badge/tests-188%20passing-brightgreen)](.github/workflows/ci.yml)
[![CI](https://github.com/faithsaly5-stack/TextSurgeon/actions/workflows/ci.yml/badge.svg)](https://github.com/faithsaly5-stack/TextSurgeon/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**English** · [فارسی](README.fa.md) · [简体中文](README.zh-CN.md)

</div>

---

## 🩻 The Problem

You paste a 5,000-word document into an AI chat and ask for *one* change. The model re-types the entire document back — slowly, expensively, and with paragraphs quietly missing.

**Text Surgeon flips the contract.** The AI never re-quotes your text. It *points* at the block to change with two short anchors, and a zero-dependency engine performs the splice on your machine:

```text
@@EDIT anchor
START-ANCHOR: The migration process begins when
END-ANCHOR: and completes the rollback safely.
<<<
The migration is now a single atomic switchover, verified end-to-end.
>>>
```

The engine resolves that selection to **exactly one** location — or refuses, with machine-actionable repair data — before a single byte moves:

```text
SELECTION CONFIRMED   anchor · lines 42–118 · 77 lines · 4,210 chars · sha256 3f6ac1b2…
```

Eleven words referenced. Seventy-seven lines replaced. The other 4,900 lines were never re-typed — so they were never at risk.

---

## ✨ Features

### 1. 🎯 Precision Surgeon Selection Engine
* **Statistical Anchors (`anchor`)**: Selects blocks of any size (10 lines or 1,000 lines) by quoting only the first ~5–10 words and last ~5–10 words.
* **Tag Markers (`tags`)**: Surgically modifies regions delimited by `[START_EDIT]` / `[END_EDIT]` comment tags.
* **Context Neighborhoods (`context`)**: Identifies repeated boilerplate by looking at surrounding lines.
* **Verbatim Search (`verbatim`)**: Fast exact search-and-replace for micro edits.

### 2. 🤖 Autonomous Multi-Round AI Agent (Auto-Pilot)
* **Goal-Driven Execution**: Provide a prompt, and the agent autonomously plans, writes, verifies, and iteratively refines files until tests pass or artifacts are created.
* **Self-Healing Loop**: If a script fails (e.g. missing package or syntax error), the engine catches the error, auto-installs missing dependencies via `pip`, and generates dynamic diagnostic repair prompts.
* **Workspace Sandboxing**: Isolated execution with path traversal protection and snapshot rollback.

### 3. 📦 Scalable Extensible Skills Engine
* **Dynamic Skill Discovery**: Drop any skill directory with a `SKILL.md` into the `skills/` folder to instantly empower the agent.
* **Built-in Production Skills**:
  * 📊 **Office CLI & PowerPoint Maker** (`python-pptx` automated deck creation)
  * 📈 **Excel Data Analyst** (`openpyxl` spreadsheets & charts)
  * 📝 **Docx Report Builder** (`python-docx` styled documents)
  * 🌐 **Web Scraper & Data Extractor** (`beautifulsoup4` + `requests`)
  * ⚡ **FastAPI High-Performance Backend**
  * 🤖 **Telegram Bot Builder** (`python-telegram-bot`)

### 4. 🔑 Resilient Multi-Provider API Calling & Key Rotation
* **Supported Providers**: OpenAI, Google Gemini, Anthropic Claude, Groq, DeepSeek, OpenRouter, and local Ollama (`llama3`, `qwen2.5-coder`).
* **Automatic Key Rotation**: Supports comma-separated API keys with instant failover on rate limits (`HTTP 429` / quota errors).

### 5. 🌍 Built for Multilingual & RTL Text
* **Invisible-elastic matching**: Transparent handling of Persian/Arabic zero-width non-joiners (ZWNJ), soft hyphens, and bidirectional isolate marks.
* **Confusable character folding**: Folds Arabic vs Persian character variants (Kaf, Yeh, Digits) for search without altering untouched file bytes.

---

## 🚀 Quick Start

### Windows (One-Click):
Double-click **`Start-Text-Surgeon.bat`**. The operating room opens at **`http://127.0.0.1:8765`**.

### macOS / Linux:
```bash
python3 surgeon_web.py
```

> **Zero External Dependencies Required for Core**: Python 3.8+ standard library only.

---

## 🔐 Configuring API Keys

### Option A: Via the Web UI
1. Open `http://127.0.0.1:8765` in your browser.
2. Switch to **🤖 Agent Mode** in the header.
3. In the **Auto-Pilot Engine** panel, choose your AI provider and paste your API key(s).

### Option B: Via `.env` File
```bash
cp .env.example .env
```
Edit `.env` with your API keys:
```env
OPENAI_API_KEY=sk-proj-your_key_1, sk-proj-your_key_2
GEMINI_API_KEY=your_gemini_key
ANTHROPIC_API_KEY=sk-ant-your_anthropic_key
GROQ_API_KEY=gsk_your_groq_key
```

---

## 🐙 One-Click GitHub Deployment

Push your local updates to GitHub with sensitive API keys and personal files protected by `.gitignore`:

### Windows:
Double-click **`deploy_to_github.bat`**.

### macOS / Linux:
```bash
chmod +x deploy_to_github.sh
./deploy_to_github.sh
```

---

## 🧪 Automated Tests

188 tests covering 100% of engine mechanics, CLI, Web REST APIs, and JS port:

```bash
# Python Engine & CLI Tests (115 tests)
python -m unittest test_surgeon_engine.py test_text_surgeon.py

# Multi-Round Agent & Skills Integration Tests (55 tests)
python test_surgeon_agent.py

# JavaScript Anchor Port Parity (18 tests)
node test_surgeon_anchor.js
```

---

## 🗂 Project Anatomy

```text
TextSurgeon/
├── Start-Text-Surgeon.bat   # One-click Windows launcher
├── deploy_to_github.bat     # One-click Windows GitHub deployment
├── deploy_to_github.sh      # One-click Unix GitHub deployment
├── .env.example             # Safe environment configuration template
├── .gitignore               # Strict security exclusion rules
├── text_surgeon.py          # Workflow core & CLI
├── surgeon_engine.py        # Selection & splice engine (Protocol v2)
├── surgeon_agent.py         # Autonomous multi-round agent & skills engine
├── surgeon_web.py           # Standard-library web server & REST APIs
├── surgeon_ui.html          # Interactive Web Operating Room (EN/FA, RTL)
├── surgeon_anchor.js        # JavaScript port of anchor selection
├── skills/                  # Extensible skills directory
│   ├── docx_report_builder/
│   ├── excel_data_analyst/
│   ├── fastapi_backend/
│   ├── officecli/
│   ├── powerpoint_maker/
│   ├── telegram_bot/
│   └── web_scraper/
├── docs/assets/banner.jpg   # Project banner
├── SCHEMA.md                # Surgeon Protocol v2 specification
├── test_surgeon_engine.py   # Core engine test suite
├── test_text_surgeon.py     # CLI integration test suite
├── test_surgeon_agent.py    # Agent & skills test suite
└── test_surgeon_anchor.js   # JS engine test suite
```

---

## 🔒 Privacy & Security

Everything runs locally on `127.0.0.1`. Your documents are never uploaded anywhere by this tool. The only external network requests are the AI API calls you explicitly trigger in Agent Mode.

## 📜 License

[MIT License](LICENSE) — free to use, modify, and distribute.

---

<div align="center">

**If Text Surgeon saved your document, leave a ⭐ on GitHub!**

*Point, don't quote.*

</div>
