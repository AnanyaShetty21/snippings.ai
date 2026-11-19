import sqlite3
import io
import time
import hashlib
import platform
import os
import shutil
import subprocess
from datetime import datetime
from threading import Thread
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageGrab, ImageTk
import pyperclip
from transformers import pipeline, BlipProcessor, BlipForConditionalGeneration
import torch

from embedding_service import EmbeddingService
from retrieval_service import RetrievalService
from query_service import QueryService
import credential_service

# Operating System Detection
CURRENT_OS = platform.system()
IS_WINDOWS = CURRENT_OS == "Windows"
IS_LINUX = CURRENT_OS == "Linux"

# Platform-specific imports with graceful fallbacks
if IS_WINDOWS:
    try:
        import win32clipboard
        import win32con
        import win32gui
    except ImportError:
        win32clipboard = None
        win32con = None
        win32gui = None
else:
    win32clipboard = None
    win32con = None
    win32gui = None

# Linux X11 bindings for window management and global hotkeys
if IS_LINUX:
    try:
        import Xlib
        import Xlib.display
        import Xlib.X
        import Xlib.XK
    except ImportError:
        Xlib = None
else:
    Xlib = None

# Optional hotkey library
try:
    import keyboard
except ImportError:
    keyboard = None

# Optional process/system utilities
try:
    import psutil
except ImportError:
    psutil = None

# Optional sentence transformers
try:
    from sentence_transformers import SentenceTransformer, util
except ImportError:
    SentenceTransformer = None
    util = None


last_window = ""


class X11HotkeyListener(Thread):
    """Listens for global hotkey Ctrl+Shift+Q on Linux under X11 without requiring root."""
    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.running = True
        self.disp = None

    def run(self):
        if not Xlib:
            return
        try:
            self.disp = Xlib.display.Display()
            root = self.disp.screen().root
            keysym = Xlib.XK.string_to_keysym('q')
            keycode = self.disp.keysym_to_keycode(keysym)
            # Support variations with NumLock and CapsLock
            modifiers = [
                Xlib.X.ControlMask | Xlib.X.ShiftMask,
                Xlib.X.ControlMask | Xlib.X.ShiftMask | Xlib.X.Mod2Mask,
                Xlib.X.ControlMask | Xlib.X.ShiftMask | Xlib.X.LockMask,
                Xlib.X.ControlMask | Xlib.X.ShiftMask | Xlib.X.Mod2Mask | Xlib.X.LockMask,
            ]
            for mod in modifiers:
                root.grab_key(keycode, mod, True, Xlib.X.GrabModeAsync, Xlib.X.GrabModeAsync)
            self.disp.sync()

            while self.running:
                event = self.disp.next_event()
                if event.type == Xlib.X.KeyPress:
                    if self.callback:
                        self.callback()
        except Exception:
            pass

    def stop(self):
        self.running = False
        if self.disp:
            try:
                self.disp.close()
            except Exception:
                pass

class ClipboardDatabase:    
    def __init__(self, db_name="smart_clipboard.db"):
        self.db_name = db_name
        self.embedding_service = EmbeddingService()
        self.retrieval_service = RetrievalService(db_name, self.embedding_service)
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clipboard_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, text TEXT, summary TEXT, image BLOB, caption TEXT, type1 TEXT,
                type2 TEXT, type3 TEXT, source TEXT NOT NULL, timestamp DATETIME
            )""")
        
        conn.commit()
        conn.close()

        # Initialize FTS5 and embeddings database tables
        self.retrieval_service.init_database()

        # Asynchronously sync any unindexed historical entries in the background
        Thread(target=self.embedding_service.sync_all, args=(self.db_name,), daemon=True).start()
    
    def insert_item(self, text=None, summary=None, image=None, caption=None, type1=None, type2=None, type3=None, source="Unknown", timestamp=None):
        if timestamp is None:
            timestamp = datetime.now()
        
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO clipboard_items 
            (text, summary, image, caption, type1, type2, type3, source, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (text, summary, image, caption, type1, type2, type3, source, timestamp))
        
        conn.commit()
        item_id = cursor.lastrowid
        conn.close()

        # Incremental indexing for Ask My Clipboard
        try:
            conn_idx = sqlite3.connect(self.db_name)
            self.embedding_service.index_item(conn_idx, item_id, text, summary, caption)
            conn_idx.close()
        except Exception as e:
            print(f"Incremental indexing error: {e}")

        return item_id
    
    def get_all_items(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, text, summary, image, caption, type1, type2, type3, source, timestamp
            FROM clipboard_items
            ORDER BY timestamp DESC
        """)
        
        items = cursor.fetchall()
        conn.close()
        return items
    
    def get_items_by_type(self, type_filter):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, text, summary, image, caption, type1, type2, type3, source, timestamp
            FROM clipboard_items
            WHERE type1 = ? OR type2 = ? OR type3 = ?
            ORDER BY timestamp DESC
        """, (type_filter, type_filter, type_filter))
        
        items = cursor.fetchall()
        conn.close()
        return items
    
    def search_items(self, query):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        search_pattern = f"%{query}%"
        cursor.execute("""
            SELECT id, text, summary, image, caption, type1, type2, type3, source, timestamp
            FROM clipboard_items
            WHERE text LIKE ? OR summary LIKE ? OR caption LIKE ?
            ORDER BY timestamp DESC
        """, (search_pattern, search_pattern, search_pattern))
        
        items = cursor.fetchall()
        conn.close()
        return items
    
    def delete_item(self, item_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM clipboard_items WHERE id = ?", (item_id,))
        cursor.execute("DELETE FROM snippet_embeddings WHERE item_id = ?", (item_id,))
        
        conn.commit()
        conn.close()


class AIModels:    
    def __init__(self, cache_dir=None):
        # Resolve cache directory (supports explicit path, environment variable, or OS standard Hugging Face cache)
        if cache_dir is None:
            cache_dir = os.environ.get("SNIPPINGS_CACHE_DIR", None)
        
        self.cache_dir = os.path.abspath(cache_dir) if cache_dir else None
        
        resolved_cache_info = self.cache_dir
        if not resolved_cache_info:
            try:
                from huggingface_hub.constants import HUGGINGFACE_HUB_CACHE
                resolved_cache_info = HUGGINGFACE_HUB_CACHE
            except Exception:
                resolved_cache_info = os.path.expanduser("~/.cache/huggingface/hub")
        
        print(f"Loading AI models (Cache directory: {resolved_cache_info})")
        device = 0 if torch.cuda.is_available() else -1
        
        # 1. Type classifier (BART Large MNLI)
        self.classifier = self._load_pipeline(
            "zero-shot-classification",
            model_name="facebook/bart-large-mnli",
            device=device
        )
        
        # Zero-shot classification topic labels
        self.topic_labels = [
            "code", "email", "url", "note", "idea", "news", "recipe", "shopping", "phone_number", "location",
            "password", "credentials", "business", "academic"
        ]
        
        # 2. Summarizer (BART Large CNN)
        self.summarizer = self._load_pipeline(
            "summarization",
            model_name="facebook/bart-large-cnn",
            device=device
        )
        
        # 3. Image caption generator (Salesforce BLIP)
        self.caption_processor, self.caption_model = self._load_blip(
            "Salesforce/blip-image-captioning-base"
        )
        if torch.cuda.is_available():
            self.caption_model = self.caption_model.to("cuda")
        
        print("AI models loaded successfully (cached)")

    def _load_pipeline(self, task, model_name, device=-1):
        model_kwargs = {}
        if self.cache_dir:
            model_kwargs["cache_dir"] = self.cache_dir
            os.makedirs(self.cache_dir, exist_ok=True)

        # Attempt to load strictly from local cache first (offline-friendly & fast)
        try:
            local_kwargs = dict(model_kwargs)
            local_kwargs["local_files_only"] = True
            pipe = pipeline(task, model=model_name, device=device, model_kwargs=local_kwargs)
            print(f"  - Loaded '{model_name}' from local cache")
            return pipe
        except Exception:
            print(f"  - Model '{model_name}' not in local cache; downloading and caching...")
            pipe = pipeline(task, model=model_name, device=device, model_kwargs=model_kwargs)
            print(f"  - Downloaded and cached '{model_name}'")
            return pipe

    def _load_blip(self, model_name):
        kwargs = {}
        if self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
            os.makedirs(self.cache_dir, exist_ok=True)

        # Attempt to load strictly from local cache first
        try:
            processor = BlipProcessor.from_pretrained(model_name, local_files_only=True, **kwargs)
            model = BlipForConditionalGeneration.from_pretrained(model_name, local_files_only=True, **kwargs)
            print(f"  - Loaded '{model_name}' from local cache")
            return processor, model
        except Exception:
            print(f"  - Model '{model_name}' not in local cache; downloading and caching...")
            processor = BlipProcessor.from_pretrained(model_name, local_files_only=False, **kwargs)
            model = BlipForConditionalGeneration.from_pretrained(model_name, local_files_only=False, **kwargs)
            print(f"  - Downloaded and cached '{model_name}'")
            return processor, model
    
    def classify_text(self, text):
        try:
            text_sample = text[:1000]
            result = self.classifier(text_sample, self.topic_labels, multi_label=True)
            
            top_labels = result['labels'][:3]
            return (top_labels[0] if len(top_labels) > 0 else None,
                   top_labels[1] if len(top_labels) > 1 else None,
                   top_labels[2] if len(top_labels) > 2 else None)
        except Exception as e:
            print(e)
            return "general", None, None
    
    def summarize_text(self, text):
        try:
            word_count = len(text.split())
            if word_count < 100:
                return None
            
            max_length = min(1024, len(text))
            text_to_summarize = text[:max_length]
            summary = self.summarizer(text_to_summarize, max_length=150, min_length=50, do_sample=False)
            return summary[0]['summary_text']
        except Exception as e:
            print(e)
            return None
    
    def generate_image_caption(self, image):
        try:
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            inputs = self.caption_processor(image, return_tensors="pt")
            out = self.caption_model.generate(**inputs, max_length=50)
            caption = self.caption_processor.decode(out[0], skip_special_tokens=True)
            return caption
        except Exception as e:
            print(e)
            return "Error"
    


class ClipboardMonitor:    
    def __init__(self, database, ai_models, gui_callback=None):
        self.database = database
        self.ai_models = ai_models
        self.gui_callback = gui_callback
        self.last_text_content = None  
        self.last_image_hash = None  
        self.monitoring = False

    
    def get_active_window_title(self):
        if IS_WINDOWS and win32gui:
            try:
                window = win32gui.GetForegroundWindow()
                global last_window
                last_window = window
                return win32gui.GetWindowText(window)
            except Exception:
                return "Unknown"
        elif IS_LINUX:
            return self._get_active_window_linux()
        return "Unknown"

    def _get_active_window_linux(self):
        # 1. Try Xlib
        if Xlib:
            try:
                disp = Xlib.display.Display()
                root = disp.screen().root
                net_active = disp.intern_atom('_NET_ACTIVE_WINDOW')
                net_name = disp.intern_atom('_NET_WM_NAME')
                wm_name = disp.intern_atom('WM_NAME')
                prop = root.get_full_property(net_active, Xlib.X.AnyPropertyType)
                if prop and prop.value and prop.value[0]:
                    win = disp.create_resource_object('window', prop.value[0])
                    name_prop = win.get_full_property(net_name, 0) or win.get_full_property(wm_name, 0)
                    if name_prop and name_prop.value:
                        val = name_prop.value
                        disp.close()
                        return val.decode('utf-8', errors='replace') if isinstance(val, bytes) else str(val)
                disp.close()
            except Exception:
                pass

        # 2. Try xprop
        if shutil.which('xprop'):
            try:
                root_out = subprocess.check_output(
                    ['xprop', '-root', '_NET_ACTIVE_WINDOW'],
                    stderr=subprocess.DEVNULL, timeout=1
                ).decode('utf-8', errors='replace')
                win_id = root_out.strip().split()[-1]
                if win_id not in ('0x0', '0') and win_id.startswith('0x'):
                    out = subprocess.check_output(
                        ['xprop', '-id', win_id, 'WM_NAME', '_NET_WM_NAME'],
                        stderr=subprocess.DEVNULL, timeout=1
                    ).decode('utf-8', errors='replace')
                    for line in out.splitlines():
                        if ' = ' in line:
                            val = line.split(' = ', 1)[1].strip()
                            if val.startswith('"') and val.endswith('"'):
                                return val[1:-1]
                            return val
            except Exception:
                pass

        # 3. Try xdotool
        if shutil.which('xdotool'):
            try:
                title = subprocess.check_output(
                    ['xdotool', 'getactivewindow', 'getwindowname'],
                    stderr=subprocess.DEVNULL, timeout=1
                ).decode('utf-8', errors='replace').strip()
                if title:
                    return title
            except Exception:
                pass

        return "Unknown"
    
    def get_clipboard_text(self):
        try:
            text = pyperclip.paste()
            return text if text else None
        except:
            return None
    
    def safe_open_clipboard(self):
        if not (IS_WINDOWS and win32clipboard):
            return False
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                return True
            except:
                time.sleep(0.05)
        return False
    
    def get_clipboard_image(self):
        if IS_WINDOWS and win32clipboard:
            if not self.safe_open_clipboard():
                return None
            
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
                    bmp_header = b'BM' + (len(data) + 14).to_bytes(4, 'little') + b'\x00\x00\x00\x00\x36\x00\x00\x00'
                    image = Image.open(io.BytesIO(bmp_header + data))
                    return image
            except Exception as e:
                print(e)
                return None
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
            
            return None
        elif IS_LINUX:
            return self._get_clipboard_image_linux()
        else:
            try:
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    return img
            except Exception:
                pass
            return None

    def _get_clipboard_image_linux(self):
        try:
            # 1. Wayland
            if os.getenv('WAYLAND_DISPLAY') and shutil.which('wl-paste'):
                p = subprocess.run(['wl-paste', '--list-types'], capture_output=True, text=True, timeout=1)
                if any('image/' in line.lower() for line in p.stdout.splitlines()):
                    img_p = subprocess.run(['wl-paste', '-t', 'image/png'], capture_output=True, timeout=2)
                    if img_p.returncode == 0 and img_p.stdout:
                        img = Image.open(io.BytesIO(img_p.stdout))
                        img.load()
                        return img

            # 2. X11 via xclip
            if shutil.which('xclip'):
                p = subprocess.run(['xclip', '-selection', 'clipboard', '-t', 'TARGETS', '-o'], capture_output=True, text=True, timeout=1)
                targets = p.stdout.splitlines()
                image_target = None
                for t in targets:
                    t_clean = t.strip().lower()
                    if t_clean in ('image/png', 'image/jpeg', 'image/bmp', 'image/tiff') or 'image/' in t_clean:
                        image_target = t.strip()
                        break
                if image_target:
                    img_p = subprocess.run(['xclip', '-selection', 'clipboard', '-t', image_target, '-o'], capture_output=True, timeout=2)
                    if img_p.returncode == 0 and img_p.stdout:
                        img = Image.open(io.BytesIO(img_p.stdout))
                        img.load()
                        return img
                else:
                    return None

            # 3. Pillow ImageGrab fallback
            img = ImageGrab.grabclipboard()
            if isinstance(img, Image.Image):
                img.load()
                return img
        except Exception:
            return None
        return None
    
    def compute_image_hash(self, image):
        with io.BytesIO() as buf:
            image.save(buf, format='PNG')
            return hashlib.md5(buf.getvalue()).hexdigest()
    
    def image_to_blob(self, image):
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        return img_byte_arr.getvalue()
    
    def process_clipboard_content(self):
        source = self.get_active_window_title()
        
        image = self.get_clipboard_image()
        if image:
            current_hash = self.compute_image_hash(image)
            
            if current_hash != self.last_image_hash:
                self.last_image_hash = current_hash  
                print("Processing new image")
                caption = self.ai_models.generate_image_caption(image)
                image_blob = self.image_to_blob(image)
                
                self.database.insert_item(
                    image=image_blob,
                    caption=caption,
                    source=source,
                    timestamp=datetime.now()
                )
                print(f"Image saved with caption: {caption}")
                
                if self.gui_callback:
                    self.gui_callback()
                
                return
        
        text = self.get_clipboard_text()
        
        if text and text != self.last_text_content:
            self.last_text_content = text 
            
            print("Processing new text")
            
            type1, type2, type3 = self.ai_models.classify_text(text)
            
            summary = self.ai_models.summarize_text(text)
            
            self.database.insert_item(
                text=text,
                summary=summary,
                type1=type1,
                type2=type2,
                type3=type3,
                source=source,
                timestamp=datetime.now()
            )
            print(f"Text saved with types: {type1}, {type2}, {type3}")
            
            if self.gui_callback:
                self.gui_callback()
    
    def start_monitoring(self):
        self.monitoring = True
        
        def monitor_loop():
            while self.monitoring:
                try:
                    self.process_clipboard_content()
                except Exception as e:
                    print(e)
                
                time.sleep(1)
        
        monitor_thread = Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()
        print("Clipboard monitoring started")
    
    def stop_monitoring(self):
        self.monitoring = False


class ClipboardGUI:    
    def __init__(self, database, ai_models, monitor):
        self.database = database
        self.ai_models = ai_models
        self.monitor = monitor
        self.query_service = QueryService(self.database.db_name, self.database.embedding_service)
        self.is_asking = False
        
        self.root = tk.Tk()
        self.root.title("Snippings.ai")
        self.root.geometry("980x660")
        self.root.withdraw() 
        
        self.bg_color = "#2b2d31" 
        self.secondary_bg = "#1e1f22" 
        self.card_bg = "#383a40" 
        self.fg_color = "#e3e5e8"  
        self.secondary_fg = "#b5bac1" 
        self.accent_color = "#5865f2" 
        self.accent_hover = "#4752c4" 
        self.danger_color = "#430102"  
        self.success_color = "#00616a"  
        
        self.root.configure(bg=self.bg_color)
        
        self.use_ollama_var = tk.BooleanVar(value=self.query_service.llm_service.is_available())
        self.setup_gui()
        self.filter_mode = "all"
        self.current_type_filter = None
        self.x11_hotkey_listener = None
        
        self.setup_hotkeys()
    
    def setup_hotkeys(self):
        # In-app local hotkeys
        self.root.bind('<Escape>', lambda e: self.hide_window())
        self.root.bind('<Control-Shift-Q>', lambda e: self.toggle_window())
        self.root.bind('<Control-Shift-q>', lambda e: self.toggle_window())

        hotkey_registered = False

        # Try `keyboard` library (Windows default, or Linux when run with appropriate permissions)
        if keyboard:
            try:
                keyboard.add_hotkey('ctrl+shift+q', lambda: self.root.after(0, self.toggle_window))
                hotkey_registered = True
            except Exception:
                pass

        # For Linux X11, use X11 global key grab without requiring root
        if not hotkey_registered and IS_LINUX and Xlib:
            try:
                self.x11_hotkey_listener = X11HotkeyListener(lambda: self.root.after(0, self.toggle_window))
                self.x11_hotkey_listener.start()
                hotkey_registered = True
            except Exception as e:
                print(f"X11 global hotkey setup failed: {e}")

        if not hotkey_registered:
            print("Note: Global hotkey (Ctrl+Shift+Q) could not be registered in background. In-app hotkeys and window controls remain active.")
    
    def setup_gui(self):
        title_frame = tk.Frame(self.root, bg=self.secondary_bg, height=60)
        title_frame.pack(fill=tk.X, padx=0, pady=0)
        title_frame.pack_propagate(False)
        
        title_label = tk.Label(title_frame, text="Snippings", 
                              font=("Segoe UI", 16, "bold"),
                              bg=self.secondary_bg, fg=self.fg_color)
        title_label.pack(pady=15, padx=20, anchor=tk.W)
        
        control_frame = tk.Frame(self.root, bg=self.bg_color)
        control_frame.pack(fill=tk.X, padx=20, pady=15)
        
        search_label = tk.Label(control_frame, text="Search", 
                               font=("Segoe UI", 10),
                               bg=self.bg_color, fg=self.secondary_fg)
        search_label.pack(side=tk.LEFT, padx=(0, 10))
        
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *args: self.perform_search())
        search_entry = tk.Entry(control_frame, textvariable=self.search_var,
                               font=("Segoe UI", 10), width=35,
                               bg=self.card_bg, fg=self.fg_color,
                               insertbackground=self.fg_color,
                               relief=tk.FLAT, bd=0)
        search_entry.pack(side=tk.LEFT, padx=5, ipady=8, ipadx=10)
        
        view_label = tk.Label(control_frame, text="View", 
                             font=("Segoe UI", 10),
                             bg=self.bg_color, fg=self.secondary_fg)
        view_label.pack(side=tk.LEFT, padx=(30, 10))
        
        self.all_button = tk.Button(control_frame, text="All Items", 
                              command=self.show_all_items,
                              font=("Segoe UI", 9),
                              bg=self.accent_color, fg=self.fg_color,
                              activebackground=self.accent_hover,
                              activeforeground=self.fg_color,
                              relief=tk.FLAT, bd=0, cursor="hand2")
        self.all_button.pack(side=tk.LEFT, padx=3, ipady=6, ipadx=12)
        
        self.type_button = tk.Button(control_frame, text="By Type", 
                               command=self.show_type_filter,
                               font=("Segoe UI", 9),
                               bg=self.card_bg, fg=self.fg_color,
                               activebackground=self.accent_color,
                               activeforeground=self.fg_color,
                               relief=tk.FLAT, bd=0, cursor="hand2")
        self.type_button.pack(side=tk.LEFT, padx=3, ipady=6, ipadx=12)

        self.ask_button = tk.Button(control_frame, text="", 
                               command=self.toggle_ask_view,
                               font=("Segoe UI", 9, "bold"),
                               bg=self.card_bg, fg=self.fg_color,
                               activebackground=self.accent_hover,
                               activeforeground=self.fg_color,
                               relief=tk.FLAT, bd=0, cursor="hand2")
        self.ask_button.pack(side=tk.LEFT, padx=3, ipady=6, ipadx=12)
        
        # Dedicated Ask My Clipboard Panel
        self.ask_panel = tk.Frame(self.root, bg=self.secondary_bg, padx=20, pady=12)
        
        ask_title = tk.Label(self.ask_panel, text="Ask the clipboard", 
                             font=("Segoe UI", 11, "bold"), bg=self.secondary_bg, fg=self.fg_color)
        ask_title.pack(anchor=tk.W, pady=(0, 2))
        
        ask_desc = tk.Label(self.ask_panel, text="Ask in plain English (e.g. 'Find the Python code for binary search', 'What documentation URL did I copy?', 'Where is my API key?')", 
                            font=("Segoe UI", 9), bg=self.secondary_bg, fg=self.secondary_fg)
        ask_desc.pack(anchor=tk.W, pady=(0, 8))
        
        query_row = tk.Frame(self.ask_panel, bg=self.secondary_bg)
        query_row.pack(fill=tk.X, pady=(0, 6))
        
        self.nl_query_var = tk.StringVar()
        self.nl_query_entry = tk.Entry(query_row, textvariable=self.nl_query_var,
                                       font=("Segoe UI", 10),
                                       bg=self.card_bg, fg=self.fg_color,
                                       insertbackground=self.fg_color,
                                       relief=tk.FLAT, bd=0)
        self.nl_query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7, ipadx=10, padx=(0, 10))
        self.nl_query_entry.bind('<Return>', lambda e: self.perform_ask_search())
        
        self.ask_submit_btn = tk.Button(query_row, text="Ask", 
                                        command=self.perform_ask_search,
                                        font=("Segoe UI", 9, "bold"),
                                        bg=self.accent_color, fg=self.fg_color,
                                        activebackground=self.accent_hover,
                                        activeforeground=self.fg_color,
                                        relief=tk.FLAT, bd=0, cursor="hand2")
        self.ask_submit_btn.pack(side=tk.LEFT, padx=(0, 15), ipady=5, ipadx=16)
        
        ollama_available = self.query_service.llm_service.is_available()
        self.ollama_cb = tk.Checkbutton(query_row, text="AI Summary (Ollama qwen3:4b)", 
                                        variable=self.use_ollama_var,
                                        font=("Segoe UI", 9),
                                        bg=self.secondary_bg, fg=self.fg_color,
                                        selectcolor=self.card_bg,
                                        activebackground=self.secondary_bg,
                                        activeforeground=self.fg_color)
        self.ollama_cb.pack(side=tk.LEFT)
        if not ollama_available:
            self.use_ollama_var.set(False)
            self.ollama_cb.config(state=tk.DISABLED, text="Ollama (Offline)")
        
        self.ask_status_label = tk.Label(self.ask_panel, text="Hybrid retrieval active: multi-qa-MiniLM semantic embeddings + SQLite FTS5 keyword matching", 
                                         font=("Segoe UI", 8, "italic"),
                                         bg=self.secondary_bg, fg=self.secondary_fg)
        self.ask_status_label.pack(anchor=tk.W)
        
        content_frame = tk.Frame(self.root, bg=self.bg_color)
        self.content_frame = content_frame
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)
        
        scrollbar = tk.Scrollbar(content_frame, bg=self.card_bg,
                                troughcolor=self.bg_color)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.canvas = tk.Canvas(content_frame, bg=self.bg_color,
                               yscrollcommand=scrollbar.set,
                               highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.canvas.yview)
        
        self.items_frame = tk.Frame(self.canvas, bg=self.bg_color)
        self.canvas_frame = self.canvas.create_window((0, 0), 
                                                       window=self.items_frame,
                                                       anchor='nw')
        
        self.items_frame.bind('<Configure>', 
                             lambda e: self.canvas.configure(
                                 scrollregion=self.canvas.bbox('all')))
        
        self.status_label = tk.Label(self.root, text="Ready", 
                                     font=("Segoe UI", 9),
                                     bg=self.secondary_bg, fg=self.secondary_fg,
                                     anchor=tk.W, padx=20)
        self.status_label.pack(fill=tk.X, side=tk.BOTTOM, ipady=10)
    
    def clear_items_display(self):
        for widget in self.items_frame.winfo_children():
            widget.destroy()
    
    def display_items(self, items):
        self.clear_items_display()
        
        if not items:
            no_items_label = tk.Label(self.items_frame, 
                                     text="No items found",
                                     font=("Segoe UI", 12),
                                     bg=self.bg_color, fg=self.secondary_fg)
            no_items_label.pack(pady=40)
            return
        
        for item in items:
            self.create_item_widget(item)
        
        self.status_label.config(text=f"Showing {len(items)} items")
    
    def create_item_widget(self, item):
        if isinstance(item, dict):
            item_id = item.get("id")
            text = item.get("text")
            summary = item.get("summary")
            image_blob = item.get("image")
            caption = item.get("caption")
            type1 = item.get("type1")
            type2 = item.get("type2")
            type3 = item.get("type3")
            source = item.get("source", "Unknown")
            timestamp = item.get("timestamp")
            match_confidence = item.get("match_confidence")
            is_sensitive = item.get("is_sensitive", False)
            masked_text = item.get("masked_text", text)
        else:
            item_id, text, summary, image_blob, caption, type1, type2, type3, source, timestamp = item[:10]
            match_confidence = None
            is_sensitive = credential_service.is_sensitive(text, [type1, type2, type3]) if text else False
            masked_text = credential_service.mask_snippet(text) if is_sensitive else text
        
        item_frame = tk.Frame(self.items_frame, bg=self.card_bg, 
                             relief=tk.FLAT, bd=0)
        item_frame.pack(fill=tk.X, padx=0, pady=8)
        
        header_frame = tk.Frame(item_frame, bg=self.card_bg)
        header_frame.pack(fill=tk.X, padx=15, pady=(12, 8))
        
        info_text = f"{source}  |   {timestamp}"
        info_label = tk.Label(header_frame, text=info_text, 
                             font=("Segoe UI", 9),
                             bg=self.card_bg, fg=self.secondary_fg,
                             anchor=tk.W)
        info_label.pack(side=tk.LEFT)

        if match_confidence is not None:
            match_badge = tk.Label(header_frame, text=f"🎯 {match_confidence}% Match",
                                  font=("Segoe UI", 8, "bold"),
                                  bg="#2d7d46", fg=self.fg_color,
                                  padx=6, pady=2)
            match_badge.pack(side=tk.LEFT, padx=(10, 0))

        if is_sensitive:
            sec_badge = tk.Label(header_frame, text="🔒 Sensitive Credential",
                                font=("Segoe UI", 8, "bold"),
                                bg="#b23b3b", fg=self.fg_color,
                                padx=6, pady=2)
            sec_badge.pack(side=tk.LEFT, padx=(8, 0))
        
        button_frame = tk.Frame(header_frame, bg=self.card_bg)
        button_frame.pack(side=tk.RIGHT)

        # Show / Hide toggle button for sensitive credentials
        is_revealed = [False]
        content_label_ref = [None]

        if is_sensitive and text:
            def toggle_reveal():
                is_revealed[0] = not is_revealed[0]
                if content_label_ref[0]:
                    if is_revealed[0]:
                        content_label_ref[0].config(text=text[:300] + ("..." if len(text) > 300 else ""))
                        reveal_btn.config(text="Hide")
                    else:
                        content_label_ref[0].config(text=masked_text)
                        reveal_btn.config(text="Show")

            reveal_btn = tk.Button(button_frame, text="Show",
                                   command=toggle_reveal,
                                   font=("Segoe UI", 9),
                                   bg="#4f545c", fg=self.fg_color,
                                   activebackground=self.accent_hover,
                                   activeforeground=self.fg_color,
                                   relief=tk.FLAT, bd=0, cursor="hand2")
            reveal_btn.pack(side=tk.LEFT, padx=3, ipady=4, ipadx=8)

        if text and len(text) > 300:
            view_full_btn = tk.Button(button_frame, text="View Full",
                                      command=lambda: self.show_full_snippet_dialog(text),
                                      font=("Segoe UI", 9),
                                      bg="#4f545c", fg=self.fg_color,
                                      activebackground=self.accent_hover,
                                      activeforeground=self.fg_color,
                                      relief=tk.FLAT, bd=0, cursor="hand2")
            view_full_btn.pack(side=tk.LEFT, padx=3, ipady=4, ipadx=8)
        
        copy_btn = tk.Button(button_frame, text="Copy", 
                            command=lambda: self.copy_to_clipboard(text, image_blob),
                            font=("Segoe UI", 9),
                            bg=self.success_color, fg=self.fg_color,
                            activebackground="#2d7d46",
                            activeforeground=self.fg_color,
                            relief=tk.FLAT, bd=0, cursor="hand2")
        copy_btn.pack(side=tk.LEFT, padx=3, ipady=4, ipadx=10)
        
        delete_btn = tk.Button(button_frame, text="Delete", 
                              command=lambda: self.delete_item(item_id),
                              font=("Segoe UI", 9),
                              bg=self.danger_color, fg=self.fg_color,
                              activebackground="#c03537",
                              activeforeground=self.fg_color,
                              relief=tk.FLAT, bd=0, cursor="hand2")
        delete_btn.pack(side=tk.LEFT, padx=3, ipady=4, ipadx=10)
        
        if type1 or type2 or type3:
            types_frame = tk.Frame(item_frame, bg=self.card_bg)
            types_frame.pack(fill=tk.X, padx=15, pady=(0, 8))
            
            types = [t for t in [type1, type2, type3] if t]
            for type_name in types:
                type_badge = tk.Label(types_frame, text=type_name, 
                                     font=("Segoe UI", 8),
                                     bg=self.accent_color, fg=self.fg_color,
                                     padx=8, pady=2)
                type_badge.pack(side=tk.LEFT, padx=(0, 5))
        
        content_frame = tk.Frame(item_frame, bg=self.card_bg)
        content_frame.pack(fill=tk.BOTH, padx=15, pady=(0, 12))
        
        if image_blob:
            try:
                image = Image.open(io.BytesIO(image_blob))
                image.thumbnail((200, 200))
                photo = ImageTk.PhotoImage(image)
                
                image_label = tk.Label(content_frame, image=photo, bg=self.card_bg)
                image_label.image = photo
                image_label.pack(pady=8)
                
                if caption:
                    caption_label = tk.Label(content_frame, 
                                           text=f"Caption: {caption}",
                                           font=("Segoe UI", 9, "italic"),
                                           bg=self.card_bg, fg=self.secondary_fg,
                                           wraplength=400)
                    caption_label.pack(pady=(0, 5))
            except Exception as e:
                print(e)
        
        if text:
            initial_text = masked_text if is_sensitive else (summary if summary else text)
            if len(initial_text) > 300:
                display_text = initial_text[:300] + "..."
            else:
                display_text = initial_text
            
            text_label = tk.Label(content_frame, text=display_text,
                                 font=("Segoe UI", 10),
                                 bg=self.card_bg, fg=self.fg_color,
                                 wraplength=800, justify=tk.LEFT,
                                 anchor=tk.W)
            text_label.pack(fill=tk.X, pady=5)
            content_label_ref[0] = text_label
            
            if summary and not is_sensitive:
                full_text_label = tk.Label(content_frame, 
                                          text="(Summarized - click Copy to get full text)",
                                          font=("Segoe UI", 8, "italic"),
                                          bg=self.card_bg, fg=self.secondary_fg)
                full_text_label.pack(anchor=tk.W)

    def show_full_snippet_dialog(self, text, title="Clipboard Snippet"):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("640x420")
        dialog.configure(bg=self.bg_color)
        
        header = tk.Frame(dialog, bg=self.secondary_bg, height=45)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="Full Snippet Content", font=("Segoe UI", 11, "bold"),
                 bg=self.secondary_bg, fg=self.fg_color).pack(side=tk.LEFT, padx=15, pady=10)
        
        copy_btn = tk.Button(header, text="Copy", command=lambda: pyperclip.copy(text),
                             font=("Segoe UI", 9), bg=self.success_color, fg=self.fg_color,
                             relief=tk.FLAT, bd=0, cursor="hand2")
        copy_btn.pack(side=tk.RIGHT, padx=15, pady=8, ipady=3, ipadx=10)
        
        text_frame = tk.Frame(dialog, bg=self.bg_color)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        scroll = tk.Scrollbar(text_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        text_box = tk.Text(text_frame, wrap=tk.WORD, font=("Consolas", 10),
                           bg=self.card_bg, fg=self.fg_color,
                           yscrollcommand=scroll.set, relief=tk.FLAT, bd=0)
        text_box.insert(tk.END, text)
        text_box.config(state=tk.DISABLED)
        text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=text_box.yview)
    
    def copy_to_clipboard(self, text, image_blob):
        try:
            if image_blob:
                if IS_WINDOWS and win32clipboard and win32con:
                    image = Image.open(io.BytesIO(image_blob))
                    output = io.BytesIO()
                    image.convert('RGB').save(output, 'BMP')
                    data = output.getvalue()[14:]
                    output.close()
                    
                    win32clipboard.OpenClipboard()
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_DIB, data)
                    win32clipboard.CloseClipboard()
                    self.status_label.config(text="Image copied to clipboard!")
                elif IS_LINUX:
                    if self._copy_image_to_clipboard_linux(image_blob):
                        self.status_label.config(text="Image copied to clipboard!")
                    else:
                        self.status_label.config(text="Failed to copy image: please install xclip or wl-clipboard")
                else:
                    self.status_label.config(text="Image copying not supported on this OS")
            elif text:
                pyperclip.copy(text)
                self.status_label.config(text="Text copied to clipboard!")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _copy_image_to_clipboard_linux(self, image_blob):
        if os.getenv('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
            try:
                p = subprocess.Popen(['wl-copy', '-t', 'image/png'], stdin=subprocess.PIPE)
                p.communicate(input=image_blob)
                return p.returncode == 0
            except Exception:
                pass
        if shutil.which('xclip'):
            try:
                p = subprocess.Popen(['xclip', '-selection', 'clipboard', '-t', 'image/png'], stdin=subprocess.PIPE)
                p.communicate(input=image_blob)
                return p.returncode == 0
            except Exception:
                pass
        return False
    
    def delete_item(self, item_id):
        self.database.delete_item(item_id)
        self.refresh_display()
        self.status_label.config(text="Item deleted")
    
    def show_all_items(self):
        self.filter_mode = "all"
        self.ask_panel.pack_forget()
        self.all_button.config(bg=self.accent_color)
        self.type_button.config(bg=self.card_bg)
        self.ask_button.config(bg=self.card_bg)
        items = self.database.get_all_items()
        self.display_items(items)
    
    def show_type_filter(self):
        self.ask_panel.pack_forget()
        self.all_button.config(bg=self.card_bg)
        self.type_button.config(bg=self.accent_color)
        self.ask_button.config(bg=self.card_bg)
        items = self.database.get_all_items()
        types = set()
        for item in items:
            type1, type2, type3 = item[5], item[6], item[7]
            if type1:
                types.add(type1)
            if type2:
                types.add(type2)
            if type3:
                types.add(type3)
        
        if not types:
            messagebox.showinfo("No Types", "No classified items found")
            return
        
        dialog = tk.Toplevel(self.root)
        dialog.title("Select Type")
        dialog.geometry("350x450")
        dialog.configure(bg=self.bg_color)
        
        tk.Label(dialog, text="Select a type to filter:",
                font=("Segoe UI", 12, "bold"),
                bg=self.bg_color, fg=self.fg_color).pack(pady=15)
        
        listbox = tk.Listbox(dialog, font=("Segoe UI", 10),
                            bg=self.card_bg, fg=self.fg_color,
                            selectbackground=self.accent_color,
                            selectforeground=self.fg_color,
                            relief=tk.FLAT, bd=0)
        listbox.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)
        
        for type_name in sorted(types):
            listbox.insert(tk.END, type_name)
        
        def on_select():
            selection = listbox.curselection()
            if selection:
                selected_type = listbox.get(selection[0])
                self.filter_mode = "type"
                self.current_type_filter = selected_type
                filtered_items = self.database.get_items_by_type(selected_type)
                self.display_items(filtered_items)
                dialog.destroy()
        
        tk.Button(dialog, text="Apply Filter", command=on_select,
                 font=("Segoe UI", 10),
                 bg=self.accent_color, fg=self.fg_color,
                 activebackground=self.accent_hover,
                 activeforeground=self.fg_color,
                 relief=tk.FLAT, bd=0, cursor="hand2").pack(pady=15, ipady=8, ipadx=20)

    def toggle_ask_view(self):
        self.filter_mode = "ask"
        self.all_button.config(bg=self.card_bg)
        self.type_button.config(bg=self.card_bg)
        self.ask_button.config(bg=self.accent_color)
        self.ask_panel.pack(fill=tk.X, padx=20, pady=(0, 10), before=self.content_frame)
        self.nl_query_entry.focus()
        self.status_label.config(text="Ask My Clipboard active: enter natural language queries")

    def perform_ask_search(self):
        query = self.nl_query_var.get().strip()
        if not query:
            self.ask_status_label.config(text="Please enter a question or query above.")
            return

        self.ask_submit_btn.config(state=tk.DISABLED)
        self.ask_status_label.config(text="⏳ Searching clipboard with hybrid semantic embeddings + keyword matching...")

        def _search_worker():
            try:
                res = self.query_service.ask(query, use_llm=self.use_ollama_var.get())
                self.root.after(0, lambda: self._on_ask_results(res))
            except Exception as e:
                self.root.after(0, lambda: self._on_ask_error(str(e)))

        Thread(target=_search_worker, daemon=True).start()

    def _on_ask_results(self, res):
        self.ask_submit_btn.config(state=tk.NORMAL)
        results = res.get("results", [])
        elapsed = res.get("execution_time_ms", 0)
        self.ask_status_label.config(text=f"Retrieved {len(results)} matches in {elapsed}ms")
        self.display_ask_results(res)

    def _on_ask_error(self, err_msg):
        self.ask_submit_btn.config(state=tk.NORMAL)
        self.ask_status_label.config(text=f"Search error: {err_msg}")
        messagebox.showerror("Search Error", f"Error during retrieval: {err_msg}")

    def display_ask_results(self, res):
        self.clear_items_display()
        results = res.get("results", [])
        answer = res.get("answer", "")
        llm_used = res.get("llm_used", False)

        # 1. AI Grounded Answer Card (if available)
        if answer and (llm_used or results):
            ai_card = tk.Frame(self.items_frame, bg="#202225", relief=tk.FLAT, bd=0, padx=15, pady=12)
            ai_card.pack(fill=tk.X, padx=0, pady=(0, 12))
            
            ai_title = tk.Label(ai_card, text="💡 Ask My Clipboard Insight" if llm_used else "🔍 Search Results Summary",
                                font=("Segoe UI", 10, "bold"),
                                bg="#202225", fg="#5865f2" if llm_used else self.secondary_fg)
            ai_title.pack(anchor=tk.W)
            
            ai_text = tk.Label(ai_card, text=answer,
                               font=("Segoe UI", 10),
                               bg="#202225", fg=self.fg_color,
                               wraplength=850, justify=tk.LEFT)
            ai_text.pack(fill=tk.X, anchor=tk.W, pady=(5, 0))

        # 2. Results list
        if not results:
            no_items_label = tk.Label(self.items_frame, 
                                     text="No matching clipboard items found.\nTry broader natural language terms or check 'All Items'.",
                                     font=("Segoe UI", 11),
                                     bg=self.bg_color, fg=self.secondary_fg)
            no_items_label.pack(pady=40)
            self.status_label.config(text="No matches found")
            return

        for item in results:
            self.create_item_widget(item)

        self.status_label.config(text=f"Showing top {len(results)} relevant matches")
    
 
    
    def perform_search(self):
        query = self.search_var.get().strip()
        
        if not query:
            self.show_all_items()
            return
        
        items = self.database.search_items(query)
        self.display_items(items)
    
    def refresh_display(self):
        if self.filter_mode == "all":
            self.show_all_items()
        elif self.filter_mode == "type" and self.current_type_filter:
            filtered_items = self.database.get_items_by_type(self.current_type_filter)
            self.display_items(filtered_items)
    
    def toggle_window(self):
        if self.root.state() == 'withdrawn':
            self.show_window()
        else:
            self.hide_window()
    
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_display()
    
    def hide_window(self):
        self.root.withdraw()
    
    def destroy(self):
        if self.x11_hotkey_listener:
            self.x11_hotkey_listener.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.destroy()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Snippings.ai - Cross-Platform AI Clipboard Manager")
    parser.add_argument("--cache-dir", default=None, help="Directory to cache downloaded AI models")
    args, _ = parser.parse_known_args()

    print(f"Starting snippings.ai (Detected OS: {CURRENT_OS})")
    print("\nInitializing database")
    database = ClipboardDatabase()
    print("\nLoading AI models")
    ai_models = AIModels(cache_dir=args.cache_dir)
    print("\nSetting up GUI")
    gui = None
    
    def gui_update_callback():
        if gui:
            gui.root.after(100, gui.refresh_display)

    print("\nStarting clipboard monitor")
    monitor = ClipboardMonitor(database, ai_models, gui_update_callback)
    gui = ClipboardGUI(database, ai_models, monitor)
    
    monitor.start_monitoring()
    print("Clipboard monitoring active")
    
    try:
        gui.run()
    except KeyboardInterrupt:
        print("\n\nShutting down")
        if gui:
            gui.destroy()
        monitor.stop_monitoring()


if __name__ == "__main__":
    main()