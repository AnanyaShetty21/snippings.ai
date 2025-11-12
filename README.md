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
| **Clipboard Access** | pyperclip, win32clipboard, keyboard |
| **AI Models** | Hugging Face Transformers, Sentence Transformers |
| **Image Captioning** | Salesforce BLIP |
| **Text Classification** | Facebook BART-Large-MNLI |
| **Text Summarization** | Facebook BART-Large-CNN |

---

## 🧠 How It Works

1. **Clipboard Monitoring**
   - Continuously checks for new text or images on the clipboard.
   - Detects if copied content is new via hashing and comparison.

2. **AI Processing**
   - For text:
     - Classifies content type using Zero-Shot Classification.
     - Generates summaries for long text.
   - For images:
     - Extracts captions using BLIP.

3. **Data Storage**
   - Saves entries (text/image, summary/caption, types, timestamp, source) in SQLite.

4. **GUI Interface**
   - Displays stored items in visually distinct cards.
   - Allows copy-back, delete, search, and filtering.

---

## 🖥️ Installation

### Prerequisites
- Python **3.8+**
- Windows OS (for `win32clipboard`, `win32con`, and `win32gui`)
- At least **8GB RAM** recommended (for loading AI models)

### Clone the Repository
```bash
git clone https://github.com/yourusername/snippings-ai.git
cd snippings-ai
