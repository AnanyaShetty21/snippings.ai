# 🧠 Snippings.ai

Snippings.ai is an **AI-powered clipboard manager** that automatically detects, classifies, summarizes, and stores your clipboard content — both text and images — with smart categorization and an intuitive GUI.  

Whether you copy code, notes, URLs, screenshots, or documents, Snippings.ai automatically organizes them and lets you search or filter them effortlessly.

---

## ✨ Features

### 🧩 Clipboard Intelligence
- Automatically monitors clipboard activity (text + images).
- Detects content type (e.g., *code, URL, email, note, idea, academic, recipe*, etc.) using **Zero-Shot Classification**.
- Summarizes lengthy copied text using **AI summarization**.
- Generates captions for copied images using **BLIP Image Captioning**.

### 🖼️ Smart Storage
- Stores all clipboard entries in a **SQLite database** (`smart_clipboard.db`).
- Keeps text, summaries, image blobs, and AI-generated captions.
- Includes timestamps and source application name.

### 🔍 Easy Management
- View all clipboard history in a **beautiful Tkinter GUI**.
- Search through your clipboard text and captions.
- Filter items by type (e.g., only show code or URLs).
- Copy or delete items directly from the interface.
- Toggle visibility instantly with a keyboard shortcut.

### ⚙️ Hotkeys
| Shortcut | Action |
|-----------|--------|
| **Ctrl + Shift + Q** | Toggle the clipboard window |
| **Esc** | Hide window |

---

## 🧰 Tech Stack

| Component | Library |
|------------|----------|
| **GUI** | Tkinter, PIL (ImageTk) |
| **Database** | SQLite3 |
| **Clipboard Access** | pyperclip, win32clipboard (Windows), xclip / wl-clipboard (Linux) |
| **Window & Hotkey Tracking** | win32gui & keyboard (Windows), python-xlib & xprop (Linux) |
| **AI Models** | Hugging Face Transformers, Sentence Transformers |
| **Image Captioning** | Salesforce BLIP |
| **Text Classification** | Facebook BART-Large-MNLI |
| **Text Summarization** | Facebook BART-Large-CNN |

---

## 🧠 How It Works

1. **OS Detection & Platform Adapter**
   - Automatically detects whether the host operating system is Windows or Linux at startup.
   - Transparently routes clipboard access, active window title tracking, and global hotkeys to the appropriate native APIs.

2. **Clipboard Monitoring**
   - Continuously checks for new text or images on the clipboard.
   - Detects if copied content is new via hashing and comparison.

3. **AI Processing**
   - For text:
     - Classifies content type using Zero-Shot Classification.
     - Generates summaries for long text.
   - For images:
     - Extracts captions using BLIP.

4. **Data Storage**
   - Saves entries (text/image, summary/caption, types, timestamp, source) in SQLite.

5. **GUI Interface**
   - Displays stored items in visually distinct cards.
   - Allows copy-back, delete, search, and filtering.

---

## 🖥️ Installation

### Prerequisites
- Python **3.8+**
- **Operating System:** Windows or Linux (auto-detected)
- At least **8GB RAM** recommended (for loading AI models)

#### Linux System Packages (Recommended)
For clipboard and window management on Linux:
- **Debian / Ubuntu:**
  ```bash
  sudo apt-get install xclip x11-utils python3-tk
  ```
  *(For Wayland sessions, `wl-clipboard` is supported as well)*
- **Arch Linux:**
  ```bash
  sudo pacman -S xclip xorg-xprop tk
  ```
- **Fedora / RHEL:**
  ```bash
  sudo dnf install xclip xprop python3-tkinter
  ```

### Clone and Install
```bash
git clone https://github.com/yourusername/snippings-ai.git
cd snippings-ai
pip install -r requirements.txt
```

### Run Snippings.ai
```bash
python snippings.py
```

### 📦 Model Caching
Models are downloaded once on first launch and cached persistently on disk for both Windows and Linux:
- **Default cache locations:**
  - **Linux:** `~/.cache/huggingface/hub/`
  - **Windows:** `C:\Users\<username>\.cache\huggingface\hub\`
- **Custom cache location:**
  You can specify a custom cache directory using the `--cache-dir` flag or the `SNIPPINGS_CACHE_DIR` environment variable:
  ```bash
  python snippings.py --cache-dir ./models_cache
  ```
  Or via environment variable:
  ```bash
  export SNIPPINGS_CACHE_DIR="./models_cache"  # Linux / macOS
  set SNIPPINGS_CACHE_DIR=.\models_cache        # Windows CMD
  $env:SNIPPINGS_CACHE_DIR=".\models_cache"     # Windows PowerShell
  ```
- **Offline / Local-First Loading:** On subsequent launches, Snippings.ai automatically loads cached models directly from local disk without making redundant network calls.
