# ✂️ Text Surgeon v2.3 — Precision Splice Editor & Autonomous AI Agent

> **A production-ready local AI Agent & Precision Text Editing Suite** with autonomous self-healing execution, modular domain skills, multi-key API rotation, and 100% token-efficient anchor editing. Zero third-party dependencies required for core execution.

---

## 🌟 Key Features

### 🤖 1. Autonomous Auto-Pilot Engine (Self-Healing Loop)
* **Plan-Execute-Verify Loop**: Give the agent a high-level goal. It designs multi-file plans, writes full code, executes setup commands, and runs your project automatically.
* **Automatic Pip Dependency Repair**: If execution fails due to a missing Python module (e.g. `No module named 'pandas'`), the engine automatically installs the missing package via `pip` and re-runs the code without breaking.
* **Closed-Loop Error Repair**: Terminal stderr logs and stack traces are captured and injected back into the LLM prompt for multi-round self-healing.

### 🧩 2. Modular Domain Skills Subsystem
* **Scalable Skill Architecture**: Extend agent capabilities over time using YAML-frontmatter `SKILL.md` files.
* **Built-in Domain Skills**:
  * 📊 **PowerPoint Presentation Builder (`powerpoint_maker`)**: Widescreen 16:9 slides, modern card containers, auto-fitting typography, and RTL/Farsi support using `python-pptx`.
  * 📈 **Excel & Data Analyst (`excel_data_analyst`)**: Automated data pipelines, pandas analytics, charts, openpyxl formatting.
  * 📝 **Word Report Builder (`docx_report_builder`)**: Executive reports, stylized callout blocks, tables using `python-docx`.
  * 🌐 **Web Scraper & Crawler (`web_scraper`)**: Extraction scripts using BeautifulSoup4, requests, and HTML parsers.
  * ⚡ **FastAPI REST Backend (`fastapi_backend`)**: Production-ready REST APIs, Pydantic v2 schemas, CORS.
  * 🤖 **Telegram Bot Builder (`telegram_bot`)**: Interactive Telegram bots with python-telegram-bot / aiogram.
  * 📄 **Office CLI (`officecli`)**: Single-binary manipulation of `.docx`, `.xlsx`, `.pptx`.
* **Automatic Skill Detection**: Automatically matches user prompts to domain skills or allows manual selection & URL skill imports.

### 🔑 3. Multi-Key API Rotation & Rate Limit Failover
* **Multi-Key Pool**: Configure multiple API keys per provider (comma or semicolon separated).
* **Smart Failover**: Automatically detects `HTTP 429 Rate Limits` or `401 Invalid Keys` and immediately fails over to the next healthy key in the pool.
* **Live Telemetry Badges**: Visual real-time health tracking (`Ready`, `Cooldown (Xs)`, `Invalid`) in the Web UI.

### 📁 4. Safe Projects Manager & Isolated Workspaces
* **Project Hub**: Create, switch, and manage isolated safe projects inside `projects/`.
* **Atomic Backup Snapshots**: Transactional backups saved before every file modification.
* **Environment Secret Manager**: Built-in editor for `.env` files per project.
* **Zip Export**: Export full project workspaces into `.zip` archives with a single click.

### ✂️ 5. Precision Anchor Splice Protocol (v2)
* **Anchor-Based Replacement**: Replaces multi-line code blocks using 5–10 word verbatim boundary anchors instead of rewriting whole files.
* **95%+ Token Savings**: Follow-up edits skip re-sending the document, cutting prompt sizes from thousands of tokens down to ~280 tokens.
* **First-Class Bidi & RTL**: Full support for Persian, Arabic, and multilingual texts without breaking quotation marks or layout direction.

---

## 🚀 Quick Start for First-Time Users

### 1. Prerequisites
* Python 3.8 or higher installed on your computer.
* *(Optional)* Local LLM runner like [Ollama](https://ollama.com) if running offline models (`llama3`, `qwen2.5-coder`).

### 2. Launching the App
#### Windows:
Double-click **`Start-Text-Surgeon.bat`** or run:
```cmd
Start-Text-Surgeon.bat
```

#### macOS / Linux:
```bash
python3 surgeon_web.py
```

Your default browser will automatically open:
👉 **`http://127.0.0.1:8765`**


---

## 🔐 Adding Your API Keys

You can provide your API keys in two convenient ways:

### Option A: Via the Web UI (Recommended)
1. Switch to **🤖 Agent Mode** in the Web UI header.
2. In the **Auto-Pilot Engine** panel, select your provider (OpenAI, Gemini, Anthropic, Groq, DeepSeek, OpenRouter).
3. Paste one or more API keys in the key field (comma-separated for key rotation, e.g. `sk-key1, sk-key2`).

### Option B: Via `.env` File
1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and add your keys:
   ```env
   OPENAI_API_KEY=sk-proj-your_key_1, sk-proj-your_key_2
   GEMINI_API_KEY=your_gemini_key
   ANTHROPIC_API_KEY=sk-ant-your_anthropic_key
   ```

---

## 🐙 One-Click GitHub Deployment (For Developers)

To push local updates to GitHub without accidentally exposing your private API keys or personal session data:

### On Windows:
Double-click **`deploy_to_github.bat`** or run:
```cmd
deploy_to_github.bat
```

### On macOS / Linux:
```bash
chmod +x deploy_to_github.sh
./deploy_to_github.sh
```

> 🛡️ **Built-in Security Notice**: The repository includes a pre-configured `.gitignore` that strictly excludes `.env`, `.surgeon_memory.json`, user workspace files in `projects/`, session tokens, and build logs from being pushed to public repositories.

---

## 🧪 Running the Automated Test Suite

Text Surgeon comes with 79 automated integration and unit tests:

```bash
# Test Agent Engine, Skills Subsystem, Key Manager & REST APIs (55 tests)
python test_surgeon_agent.py

# Test Core Precision Splice Engine & Protocol v2 (24 tests)
python test_text_surgeon.py
```

---

## 📜 License & Author

Built with precision for agentic coding workflows. Built using Python Standard Library components.
