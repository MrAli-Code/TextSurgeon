TEXT SURGEON v3.0 - PRECISION SURGICAL EDITING & AUTONOMOUS AGENT
==================================================================

WHAT'S NEW IN v3.0 (AUTONOMOUS AGENT MODE)
  * Mode Switcher in UI:
      - ✂️ Document Mode: The classic anchor-based surgical editor for single
        documents.
      - 🤖 Agent Mode: Autonomous multi-file coding agent.
  * Multi-File Project Workspace: Point Text Surgeon at any folder or project.
    It builds prompts containing your workspace context, parses multi-file
    plans from the LLM, transactionally creates/modifies/deletes files, executes
    your scripts, captures terminal stdout/stderr logs and created artifacts
    (e.g., PowerPoint .pptx, Excel .xlsx, data files, charts), and generates a
    closed-loop verification prompt to paste back to the AI for self-healing!
  * Zero-Dependency Runtime: Standard library Python only.
  * CLI Agent Mode:
      python3 text_surgeon.py --agent --project-dir ./my_app --goal "Build presentation generator"
      python3 text_surgeon.py --agent --project-dir ./my_app --apply ai_plan.txt --run

WHAT'S NEW IN v2.2
  * Two prompt modes (big token saver). In Step 1 you now choose:
      - "New chat"  — the prompt includes the whole document (use this the
                      first time, for a fresh AI conversation).
      - "Same chat" — a compact prompt that does NOT re-send the document,
                      because the AI already has it from earlier in the same
                      conversation. For a large file this cuts thousands of
                      tokens per follow-up (e.g. ~5,500 -> ~290 tokens).
    In "Same chat" mode, any edits you've applied since the document was
    shared are summarized in the prompt, so the AI's copy stays correct.
    CLI: add --followup to --generate.

WHAT'S NEW IN v2.1.2
  * Verification prompt is now shown as "Step 3", directly under the green
    "Edit applied" banner (it was previously below the splice cards, so on a
    large edit it looked missing). It was always generated - just buried.
  * More resilient to how AIs really reply: right-to-left responses that
    attach invisible direction marks to the <<< / >>> fences now parse
    correctly; whole-block ``` code fences and sloppy <<<< / >>>> fences are
    tolerated; and if an @@EDIT block is still malformed the app falls back
    to a JSON array in the same reply.
  * Smart-quote safety: curly quotes/apostrophes ("" '') that models
    substitute for plain ASCII ones no longer break anchor matching. Persian
    guillemets « » are preserved (never folded) because they are real
    quotation marks.

WHAT'S NEW IN v2.1
  * Native file picker. Click "Choose file..." to open your operating
    system's real Open-File dialog (the same one Chrome/Word use). No more
    typing long paths. If a machine has no desktop dialog, the app falls
    back to the built-in folder browser automatically.
  * Files workspace. Every file you open is remembered in a "Your files"
    list. Click any file to switch to it and RESUME where you left off -
    your pending change request is reloaded automatically. Each file shows
    its own badges (operation count, pending change, backup, missing) and
    an x to remove it from the list.
  * Separate memories per file. Each document keeps its own operation
    history; switching files never mixes their logs.
  * International text robustness (matters a lot for Farsi/Arabic). Anchor
    matching now transparently handles:
      - invisible characters: soft hyphens, ZWNJ/ZWJ, zero-width spaces,
        bidi marks (a doc using a soft hyphen matches an anchor typed with
        a ZWNJ, or with nothing);
      - Persian/Arabic look-alikes: Arabic kaf/yeh vs Persian keheh/farsi
        yeh, alef-maksura, teh-marbuta, and Arabic/Persian/ASCII digits;
      - Markdown emphasis: **bold** and _italic_ markers no longer block
        anchoring a word they are glued to.
    Matching is forgiving; your file's exact bytes are never rewritten
    outside the block you replace, and uniqueness is still enforced.

WHAT'S NEW IN v2 (Surgeon Protocol 2.0)
  The AI no longer re-quotes the text it wants to change. It points at a
  block with two short markers and the engine does the rest:

    anchor    Statistical Anchor Marking (the default). The block's first
              5-10 words + last 5-10 words, each verified to be unique in
              the file (exactly one match) before anything is touched.
              A 1,000-line block is replaced by referencing ~15 words.
              Ambiguous marker? The engine aborts and answers with unique
              extended anchors ("The system is" -> "The system is
              initialized by the user") so the next attempt succeeds.
    tags      You place [START_EDIT] / [END_EDIT] marker lines (in any
              comment syntax); the engine edits only what's between them.
    context   Fuzzy match of the 1-5 lines above/below the block - for
              repetitive boilerplate where no unique anchor exists.
    verbatim  v1-style exact search/replace, still ideal for micro-edits.

  Safety: selection and mutation are separate phases. Every edit gets a
  SELECTION CONFIRMED line (line range, size, SHA-256) before the file is
  written; batches are transactional, overlap-checked, and spliced
  bottom-up. Whitespace, indentation, CRLF/LF, BOM, and final-newline
  fidelity are handled by the engine, never by the AI.

  Full schema: SCHEMA.md. Old v1 JSON responses still work.

HOW TO RUN (Windows, one click)
  1. Keep all files in the same folder.
  2. Double-click "Start-Text-Surgeon.bat".
  3. A local server starts and the app opens in your browser
     (http://127.0.0.1:8765). Keep the black window open while you work,
     or stop everything with the Quit button in the page.

COMMAND LINE
  python3 text_surgeon.py notes.md --generate "Rewrite section 2"
  python3 text_surgeon.py notes.md --apply response.txt --dry-run
  python3 text_surgeon.py notes.md --apply response.txt
  python3 text_surgeon.py notes.md --suggest-anchors 120:180
  python3 text_surgeon.py notes.md --history

REQUIREMENTS
  Python 3.8 or newer (https://python.org). Nothing else - no pip packages.

FILES
  text_surgeon.py          workflow core + CLI (prompts, apply, memory log)
  surgeon_engine.py        selection & splice engine (all four strategies,
                           invisible/confusable-elastic matching)
  surgeon_anchor.js        JavaScript port of the anchor strategy
                           (Node + browser, zero dependencies)
  surgeon_web.py           local web server (standard library only)
  surgeon_ui.html          the browser UI (English / Farsi, full RTL support)
  SCHEMA.md                Surgeon Protocol v2 edit schema (JSON + Markdown)
  test_surgeon_engine.py   engine unit tests      (python3 -m unittest)
  test_text_surgeon.py     integration tests      (python3 -m unittest)
  test_surgeon_anchor.js   JS port tests          (node test_surgeon_anchor.js)
  Start-Text-Surgeon.bat   one-click launcher
  README.txt               this file

WORKFLOW
  1. Open your file (type the path or use Browse).
  2. Step 1: write your change request -> Generate prompt -> Copy ->
     paste into ChatGPT / Claude / Gemini.
  3. Step 2: paste the AI's full reply -> Preview (safe, nothing saved) ->
     Apply edit.
  4. The app resolves every selection to EXACTLY ONE location before
     touching the file, saves atomically with a .bak backup, logs the
     operation to .surgeon_memory.json, and gives you a verification
     prompt to paste back to the AI.

PRIVACY
  Everything runs on 127.0.0.1 (your machine only). Your documents are
  never uploaded anywhere by this tool.

----------------------------------------------------------------------

تکست سرجن ۲٫۱ - انتخاب و جایگزینی بر پایه لنگر
==============================================

تازه‌های نسخه ۲٫۱
  * انتخاب فایل با دیالوگ سیستم‌عامل. با کلیک روی «انتخاب فایل…» همان
    پنجرهٔ بازکردن فایلِ ویندوز/سیستم شما باز می‌شود؛ دیگر لازم نیست مسیر
    را تایپ کنید. اگر دستگاهی دیالوگ گرافیکی نداشته باشد، برنامه به‌طور
    خودکار به مرورگر پوشهٔ داخلی برمی‌گردد.
  * فضای کاری فایل‌ها. هر فایلی که باز کنید در فهرست «فایل‌های شما» ذخیره
    می‌شود. با کلیک روی هر فایل به آن جابه‌جا می‌شوید و کار را از همان‌جا
    ادامه می‌دهید — درخواست تغییرِ در جریانِ آن فایل دوباره بارگذاری
    می‌شود. هر فایل نشان‌های خودش (تعداد عملیات، تغییر در جریان، پشتیبان،
    یافت‌نشدن) و دکمهٔ × برای حذف از فهرست دارد.
  * حافظهٔ جدا برای هر فایل. تاریخچهٔ هر سند مستقل است و جابه‌جایی بین
    فایل‌ها هرگز آن‌ها را با هم مخلوط نمی‌کند.
  * پایداری متن چندزبانه (به‌ویژه فارسی/عربی). تطبیق لنگر اکنون این‌ها را
    نامرئی در نظر می‌گیرد: نیم‌فاصله و نویسه‌های صفرعرض، علامت‌های جهت‌نما؛
    و شکل‌های هم‌ریخت فارسی/عربی (ک/ك، ی/ي/ى، ة/ه) و ارقام فارسی/عربی/لاتین
    را یکسان می‌شمارد؛ و علامت‌های **پررنگ** و _کج_ مارک‌داون دیگر مانع
    لنگر انداختن روی واژهٔ چسبیده به آن‌ها نمی‌شوند. تطبیق سخت‌گیر نیست،
    اما بایت‌های فایل شما بیرون از بلوکِ جایگزین‌شده هرگز تغییر نمی‌کنند.

تازه‌های نسخه ۲ (پروتکل جراح ۲٫۰)
  هوش مصنوعی دیگر متنِ در حال تغییر را دوباره نقل نمی‌کند؛ بلکه با دو
  نشانگر کوتاه به بلوک «اشاره» می‌کند:

    anchor    نشانه‌گذاری آماری (پیش‌فرض): ۵ تا ۱۰ کلمه اول + ۵ تا ۱۰
              کلمه آخر بلوک. هر نشانگر باید دقیقاً یک بار در سند وجود
              داشته باشد؛ در غیر این صورت موتور توقف می‌کند و نشانگرهای
              یکتای پیشنهادی برمی‌گرداند.
    tags      ویرایش فقط بین خطوط [START_EDIT] و [END_EDIT] که خودتان
              گذاشته‌اید.
    context   تطبیق فازیِ خطوط بالا و پایین بلوک، برای متن‌های تکراری.
    verbatim  همان جستجو/جایگزینی دقیق نسخه ۱ برای تغییرات ریز.

  ایمنی: پیش از هر نوشتن، انتخابِ هر ویرایش با محدوده خط و اثر SHA-256
  تأیید می‌شود؛ کل دسته تراکنشی است و فاصله‌گذاری، تورفتگی و انتهای
  خطوط (CRLF/LF) را موتور حفظ می‌کند. شِمای کامل: SCHEMA.md

اجرا (ویندوز، با یک کلیک)
  ۱. همه فایل‌ها را در یک پوشه نگه دارید.
  ۲. روی «Start-Text-Surgeon.bat» دوبار کلیک کنید.
  ۳. یک سرور محلی اجرا می‌شود و برنامه در مرورگر باز می‌شود
     (http://127.0.0.1:8765). پنجره سیاه را باز نگه دارید؛ برای خروج از
     دکمه «خروج» داخل صفحه استفاده کنید.

نیازمندی
  پایتون ۳٫۸ یا جدیدتر (python.org). هیچ کتابخانه اضافه‌ای لازم نیست.

روند کار
  ۱. فایل خود را باز کنید (مسیر را بنویسید یا «مرور» بزنید).
  ۲. مرحله ۱: درخواست تغییر را بنویسید ← «ساخت پرامپت» ← «کپی» ←
     در ChatGPT / Claude / Gemini الصاق کنید.
  ۳. مرحله ۲: پاسخ کامل هوش مصنوعی را الصاق کنید ← «پیش‌نمایش» ←
     «اعمال ویرایش».
  ۴. برنامه پیش از هر تغییری مطمئن می‌شود هر انتخاب دقیقاً به یک مکان
     می‌رسد؛ سپس با نسخه پشتیبان (.bak) ذخیره می‌کند، عملیات را در
     .surgeon_memory.json ثبت می‌کند و پرامپت راستی‌آزمایی به شما
     می‌دهد.

حریم خصوصی
  همه‌چیز فقط روی 127.0.0.1 (دستگاه خود شما) اجرا می‌شود و اسناد شما
  هرگز جایی بارگذاری نمی‌شوند.
