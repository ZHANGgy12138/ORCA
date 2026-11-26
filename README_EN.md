# ORCA

[简体中文](README.md) | **English**

ORCA is a PDF translation and reading tool for Windows. Translate papers or books in batches, export translated or bilingual PDFs, and select text while reading to see its translation. ORCA uses PDFMathTranslate Next 2.9.0 and BabelDOC 0.6.2 to parse and typeset documents on your computer.

## Download and launch (Windows x64)

1. Open this repository's **[Releases](https://github.com/ZHANGgy12138/ORCA/releases)** and download **`ORCA-0.9.1-windows-x64.zip`** from the **Assets** for version 0.9.1. The portable package includes Python.
2. Find the downloaded ZIP, **right-click → Extract All**, and wait for extraction to finish.
3. Open the extracted **`ORCA`** folder and double-click **`ORCA.exe`**. Keep the executable and its **`_internal`** folder together. The window shows resource-loading progress during startup.

Launching the executable directly from a ZIP preview may cause Windows to display `Failed to load Python DLL`. Fully extract the archive as described above, then launch ORCA from the extracted folder.

## Translate your first PDF

The current interface uses Chinese labels. The instructions below include those labels so you can locate the controls.

1. Click **“选择文件” (Select files)** to choose multiple PDFs. You can add more files, and duplicates are skipped automatically. Remove files or clear the queue before starting.
2. Choose the source language, target language, and translation engine. The default is the free Bing engine, which requires an internet connection. Change engines through **“引擎设置” (Engine settings)** in the sidebar.
3. Click **“每份试译 5 页” (Try the first 5 pages of each file)** to check the results, or translate the queue using the full document or a specified page range. A specified range applies to every PDF and is validated before translation. Export a translated PDF, a bilingual PDF, or both.
4. Files are translated one at a time in queue order. A document's result folder appears in the output location after all requested PDFs for that document have been generated. If one file fails, ORCA proceeds to the next. Cancelling a queue preserves completed results. Open translations from the queue or **“最近任务” (Recent tasks)**.
5. Switch between light and dark mode in the upper-right corner. The title bar follows the interface theme. Drag the top of the window to move it, double-click it or use the upper-right controls to maximize or restore it, and resize the window from its edges. Minimizing is also supported. Recent tasks remain available after reopening ORCA.
6. Click the floating-window button to the left of Minimize to switch to a compact progress window while translation continues. It shows the current file, completed file count, overall progress, and current stage. You can cancel the queue, toggle Always on top, or return to the main window.
7. Returning to the main window restores its previous size, position, or maximized state. The floating window's position, size, and Always on top preference are retained during the current session. ORCA starts in the main window after a restart. Closing the floating window exits the application, with a confirmation prompt when a task is active.

Long documents are processed in parts of up to 50 pages. Results for scanned documents, complex formulas, and academic terminology depend on the source and the selected engine. Check important content against the original. Choosing **“翻译全文” (Translate the full document)** after a trial starts a full-document task. Existing translation caches may be reused, while saved-progress recovery uses the separate resume workflow.

The engine panel provides 23 service entries, including Bing, Google, OpenAI, DeepSeek, Gemini, DeepL, Ollama, Xinference, Azure, Tencent Cloud, Alibaba Cloud Model Studio, Tongyi Translation, ModelScope, Zhipu, and SiliconFlow. The custom OpenAI-compatible option accepts your model name, endpoint, and advanced parameters. ClaudeCode and CLITranslator, which require external command-line applications, are excluded from the portable interface.

Version 0.6.1 fixed missing dynamically loaded engine modules. The build scans source files to collect the actual implementations and stops if a required module is missing. The 23 service entries map to 15 adapter modules according to their configuration, so entry names do not necessarily correspond to module filenames. Release checks validate these mappings and loading in separate child processes. Local mock services also check requests and responses for DeepL, custom compatible endpoints, Zhipu, ModelScope, and Index. These checks cover components and call paths. Actual cloud account access, model permissions, and public free service availability require separate online verification.

**“测试连接” (Test connection)** sends a short sentence to the selected service and waits up to 30 seconds. Paid services may charge a small amount. Availability depends on the account, network, and model permissions. API keys are kept in the current session by default. To save a key, select **“在这台电脑上记住密钥” (Remember the key on this computer)** in engine settings, then click **“使用此引擎” (Use this engine)**. Saved keys are stored in Windows Credential Manager and excluded from ordinary preferences and task history. Language, theme, output location, and non-sensitive model settings are saved automatically. The application includes no developer API keys.

Document parsing and typesetting run on your computer. Text is sent to the selected translation service. The portable package includes the models and fonts, which are copied to the user's application data directory on first use. Results and logs are stored under `%LOCALAPPDATA%/ORCA` by default. You can change the output location in the application.

Since 0.6.1, full-document translation caches also use `cache/translation` under ORCA's application data directory, keeping build and worker processes separate from another installation's user cache. Caches in the old location are retained without automatic migration, so the first translation may need to request previously translated content again. Preferences, recent tasks, and existing PDF outputs remain available.

The desktop window uses Microsoft Edge WebView2. Windows computers with this runtime installed can launch ORCA directly. Version 0.9.1 is an unsigned test release, and compatibility testing across different computers is still incomplete.

## Pause and resume, tables, and images

During translation, click **“暂停并保存” (Pause and save)**. Wait until the interface confirms that progress has been saved before shutting down. The next time you open ORCA, click **“继续翻译” (Continue translation)** in **“可继续的翻译” (Resumable translations)**. Recovery uses the original engine, languages, page range, model, and output settings. If the original PDF has moved, you can locate it again. Its contents are verified before resuming. You may need to re-enter a session-only API key. Cancelling a task or deleting its recovery record discards the corresponding intermediate progress.

**“自动保存进度” (Automatically save progress)** is off by default for every new task. When enabled, returned translations are written to disk in batches. After an unexpected shutdown, work since the last save may need to be repeated. Completed parts can be reused, while unfinished parts may need to be parsed and typeset again. Only tasks saved by versions supporting recovery appear in the recovery list. Keep the application data directory and original PDFs.

For tables with clear vector borders and selectable text, ORCA extracts text by cell, joins layout-induced line breaks while preserving paragraphs and numbering, and typesets the translation inside each cell. Scanned tables, borderless tables, and complex formulas still have limitations.

When using Index, enable **“翻译图片中的文字” (Translate text in images)** to process embedded bitmap images on the selected pages. The images are sent to Bilibili. When readable text is found, ORCA adds an appendix to the output PDF with the original page number, image number, thumbnail, and translation. Small decorative images are skipped. For vector-only graphics, use **“框选图片翻译” (Select an image region to translate)** in the reader to select a region manually. Images are submitted only when you make a region selection or enable the image-translation option.

## Index-Translate free engine

In the sidebar's **“引擎设置” (Engine settings)**, select **“Index-Translate · B站免费翻译” (Index-Translate · Bilibili free translation)** and click **“使用此引擎” (Use this engine)**. An API key is not required. This integration uses Bilibili's publicly available Index-Translate-35B-A3B text model for selected-text translation and paragraph-by-paragraph PDF translation. Files are parsed locally, and text requiring translation is sent to the service. It requires neither a large local translation-model download nor a local GPU.

The adapter uses the official request headers and translation prompt, with thinking mode disabled. Long passages are split at boundaries while keeping formula and styling markers intact. Empty responses, truncated outputs, and missing markers produce errors. If a paragraph request fails during full-document translation, the task cannot be delivered as a complete translation. Check terminology, meaning, and complex layouts against the original.

The endpoint is described as a free experimental public service that requires no registration. Its maintainer documents per-IP limits of 500 requests and 120,000 tokens per minute. Service rules may change. ORCA sends paragraph requests at low concurrency and respects rate limits. See the [official project](https://github.com/bilibili/Index-Translate) and the [maintainer's service notes](https://github.com/bilibili/Index-Translate/issues/28).

## Reading and selected-text translation

Select **“阅读翻译” (Reading translation)** in the sidebar and open a PDF. Scroll continuously through pages with the mouse wheel. The displayed page number updates as you scroll, and the previous-page button, next-page button, and page-number field provide quick navigation. Zoom and Fit to width are supported, and pages retain their original colors. Typically, at most five pages near the current position are rendered, with pages containing the active selection retained as well. This helps with long documents.

Select text to display the selected service's translation in the right panel. You can copy the translation or disable automatic translation and submit selections manually. The translation panel can be collapsed or resized. Scrolling, navigating between pages, and zooming preserve the displayed translation.

The reader uses the existing language and engine settings. Each request sends only the selected text, up to 6,000 characters. Longer selections prompt you to select less text. When you make another selection, the previous request is cancelled and only the latest selection's result is shown. Each request waits up to 30 seconds. Reading translation runs independently of the full-document queue and displays its results in the panel rather than exporting a PDF.

Opening a document prepares the translation worker by initializing local dependencies without sending text or health-check requests. The worker and connection are reused for the same engine configuration. Up to 64 recent successful translations are cached in memory, allowing an identical selection with the same language and engine settings to reuse its result without a network request. Opening another document, closing the document, or exiting ORCA clears this cache. Cancelling an active network request ends its worker process, so the next request may require initialization again.

The free Bing entry uses a Bing Translator web session. It first retrieves temporary session parameters from the web page, then sends text to the translation endpoint used by that page and reads the response. No user API key is required. The PDF is read locally, while the selected text is sent to Bing. The reader briefly reuses session parameters to reduce repeated page requests. ORCA sets this reuse period to 120 seconds, which is its own policy rather than a Microsoft-guaranteed validity period. Authentication or parameter expiration can trigger one refresh. Rate limits prompt the user to try again later. Initialization, network conditions, rate limits, and splitting long passages can all introduce a delay. The endpoint alone does not establish which Microsoft model is used. Full-document translation continues to use the upstream processing workflow.

The PDF viewer is bundled with ORCA, and opening a PDF requires no internet connection. Whether translation needs a connection depends on the selected service. For scanned pages without a text layer, select Index and draw a region to recognize and translate. Encrypted PDFs must be decrypted first. Complex columns, formulas, and text encoding can affect text extraction, so check the original text shown in the right panel. The reader shows the current selection and does not save selection history or annotations.

## Updating

Updates are currently manual. Finish your task and exit ORCA, fully extract the new portable ZIP into a new folder, then run the new `ORCA.exe`. Copy the complete package and replace versions only after the application has exited.

Preferences, recent tasks, and the default output location are stored separately under `%LOCALAPPDATA%/ORCA`. Version 0.9.1 continues to read the existing data, and custom output folders remain available when replacing the application. Keys saved for the session only must be entered again. Keys stored in Windows Credential Manager are loaded automatically for the same Windows user on the same computer. Once the new version works correctly, you can remove the old application folder while retaining your documents and application data.

Find new versions and release notes on this repository's **[Releases](https://github.com/ZHANGgy12138/ORCA/releases)** page.

## Source, modifications, and license

ORCA is released under AGPL-3.0 without any warranty. See [LICENSE](LICENSE). The portable package's `SOURCE.zip` contains the source and build files for this version. The `licenses` directory records dependency and font licenses. You may obtain, modify, and redistribute the code under the license terms.

Version 0.7.1 uses the approved markings-v7-final orca logo. The canonical vector asset is `ui/assets/orca-mark-v7.svg`, which is used directly by the title bar and floating window. `scripts/make_icon.py` generates the Windows multi-size ICO from the same source. The dark interface and application icon use a light backing to preserve the original black-and-white markings.

PDF reading uses Mozilla PDF.js 6.5.438 under Apache-2.0. The bundled component's sources, version, and checksums are recorded in `ui/vendor/pdfjs/manifest.json`, with licenses in `licenses/pdfjs`. Required text-layer styles have been scoped to fit ORCA.

Upstream contributions and copyrights are retained for Byaidu, awwaawwa, and the other PDFMathTranslate Next contributors, and for funstory.ai and the BabelDOC contributors. The desktop interface, packaging entry point, and compatibility changes were added on November 16–17, 2026, under the application name ORCA.

- A standalone desktop window with file selection, page ranges, progress, cancellation, result opening, and engine switching.
- Runtime caches and configuration moved into a separate application data directory.
- Long-passage splitting, timeouts, and cache markers for Bing to avoid upstream truncation at 1,000 characters.
- Child-process cleanup after successful translation corrected to prevent false failure reports.
- bitstring pinned to 4.x with a compatible dependency set.
- DLL subdirectories retained for hyperscan and other dependencies to fix loading in translation child processes.
- Prompt error reporting for child-process crashes and cleanup after completion or cancellation before accepting a new task.
- Black-and-white light and dark themes, the curved orca logo, an integrated title bar, and native window controls.
- Multiple-file selection, queue management, sequential translation, separate document outputs, and the 20 most recent tasks.
- Restored multi-engine and custom-model adapters. The Tencent SDK is pinned to 3.1.70 and uses explicit credentials. Azure supports a user-specified region.

See the upstream projects [PDFMathTranslate Next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) and [BabelDOC](https://github.com/funstory-ai/BabelDOC).

## Build from source

Use a Python 3.12 virtual environment on Windows x64 and run the following PowerShell commands from the repository root. Replace `PATH_TO_PUBLIC_BABELDOC_ASSETS` with a directory containing the `fonts`, `models`, `cmap`, and `tiktoken` subdirectories. These resources are also available in the extracted 0.9.1 portable package at `ORCA\_internal\resources\babeldoc`.

```powershell
python -m pip install -r requirements-build.txt
$env:PYTHONPATH = "$PWD/vendor"
python scripts/prepare.py --assets PATH_TO_PUBLIC_BABELDOC_ASSETS
python scripts/vendor_pdfjs.py
python -m PyInstaller --noconfirm desktop.spec
python scripts/package.py
```

`vendor` contains the engine sources used for this build. Add it to `PYTHONPATH` before building. Use the same directory when launching from source with `$env:PYTHONPATH = "$PWD/vendor"; python app.py`.

Before release, run `ORCA.exe --engine-test`. It checks the 15 adapter modules resolved from the 23 configuration entries, loads dependencies in fresh child processes, and uses local mock services to check five categories of connection and reading requests. This check uses no cloud keys or external network access and writes its report to the test application data directory. The build file `build/bundled-module-manifest.json` confirms that the required source modules were collected.

`ORCA.exe --free-engine-test` separately sends a short connection test to Bing, Google, SiliconFlow's public free service, and Index-Translate, allowing up to 30 seconds for each. This is an online test whose results can vary with network conditions and service limits. Set separate `PDFMT_DATA_DIR` and `PDFMT_REPORT` values for both tests to keep test data separate from normal use.

For diagnostics, set `PDFMT_DATA_DIR` to an isolated test directory and run `ORCA.exe --diagnostics`. Setting `PDFMT_TEST_INPUT` and running `--self-test` translates the first page of that PDF with the free Bing engine and produces a report for inspection. This test sends the selected page's text to Bing. `--reader-test` opens the PDF, selects a short passage on the first page, and calls Bing to check the reading interface, themes, page navigation, and return from the floating window.
