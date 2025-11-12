import sqlite3
import io
import time
import hashlib
from datetime import datetime
from threading import Thread
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageGrab, ImageTk
import win32clipboard
import win32con
import keyboard
import psutil
import win32gui
import pyperclip
from transformers import pipeline, BlipProcessor, BlipForConditionalGeneration
from sentence_transformers import SentenceTransformer, util
import torch


last_window = ""

class ClipboardDatabase:    
    def __init__(self, db_name="smart_clipboard.db"):
        self.db_name = db_name
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
        
        conn.commit()
        conn.close()


class AIModels:    
    def __init__(self):
        print("Loading AI models")
        
        #type classifier
        self.classifier = pipeline("zero-shot-classification", model="facebook/bart-large-mnli")
        
        #zero shot labels
        self.topic_labels = [
            "code", "email", "url", "note", "idea", "news", "recipe", "shopping", "phone_number", "location",
            "password", "credentials", "business","academic"
        ]
        
        #summarizer
        self.summarizer = pipeline("summarization", model="facebook/bart-large-cnn")
        
        #image caption generator
        self.caption_processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
        self.caption_model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")
        
        print("AI models loaded")
    
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
        try:
            window = win32gui.GetForegroundWindow()
            global last_window
            last_window = window
            return win32gui.GetWindowText(window)
        except:
            return "Unknown"
    
    def get_clipboard_text(self):
        try:
            text = pyperclip.paste()
            return text if text else None
        except:
            return None
    
    def safe_open_clipboard(self):
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                return True
            except:
                time.sleep(0.05)
        return False
    
    def get_clipboard_image(self):
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
            win32clipboard.CloseClipboard()
        
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
        
        self.root = tk.Tk()
        self.root.title("Snippings.ai")
        self.root.geometry("900x600")
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
        
        self.setup_gui()
        self.filter_mode = "all"
        self.current_type_filter = None
        
        keyboard.add_hotkey('ctrl+shift+q', self.toggle_window)
        self.root.bind('<Escape>', lambda e: self.hide_window())
    
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
        
        all_button = tk.Button(control_frame, text="All Items", 
                              command=self.show_all_items,
                              font=("Segoe UI", 9),
                              bg=self.accent_color, fg=self.fg_color,
                              activebackground=self.accent_hover,
                              activeforeground=self.fg_color,
                              relief=tk.FLAT, bd=0, cursor="hand2")
        all_button.pack(side=tk.LEFT, padx=3, ipady=6, ipadx=12)
        
        type_button = tk.Button(control_frame, text="By Type", 
                               command=self.show_type_filter,
                               font=("Segoe UI", 9),
                               bg=self.card_bg, fg=self.fg_color,
                               activebackground=self.accent_color,
                               activeforeground=self.fg_color,
                               relief=tk.FLAT, bd=0, cursor="hand2")
        type_button.pack(side=tk.LEFT, padx=3, ipady=6, ipadx=12)
        
        
        content_frame = tk.Frame(self.root, bg=self.bg_color)
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
        item_id, text, summary, image_blob, caption, type1, type2, type3, source, timestamp = item
        
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
        
        button_frame = tk.Frame(header_frame, bg=self.card_bg)
        button_frame.pack(side=tk.RIGHT)
        
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
            display_text = summary if summary else text
            if len(display_text) > 300:
                display_text = display_text[:300] + "..."
            
            text_label = tk.Label(content_frame, text=display_text,
                                 font=("Segoe UI", 10),
                                 bg=self.card_bg, fg=self.fg_color,
                                 wraplength=800, justify=tk.LEFT,
                                 anchor=tk.W)
            text_label.pack(fill=tk.X, pady=5)
            
            if summary:
                full_text_label = tk.Label(content_frame, 
                                          text="(Summarized - click Copy to get full text)",
                                          font=("Segoe UI", 8, "italic"),
                                          bg=self.card_bg, fg=self.secondary_fg)
                full_text_label.pack(anchor=tk.W)
    
    def copy_to_clipboard(self, text, image_blob):
        try:
            if image_blob:
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
            elif text:
                pyperclip.copy(text)
                self.status_label.config(text="Text copied to clipboard!")
        except Exception as e:
            messagebox.showerror("Error", e)
    
    def delete_item(self, item_id):
        self.database.delete_item(item_id)
        self.refresh_display()
        self.status_label.config(text="Item deleted")
    
    def show_all_items(self):
        self.filter_mode = "all"
        items = self.database.get_all_items()
        self.display_items(items)
    
    def show_type_filter(self):
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
    
    def run(self):
        self.root.mainloop()


def main():
    print("Starting snippings.ai")
    print("\nInitializing database")
    database = ClipboardDatabase()
    print("\nLoading AI models")
    ai_models = AIModels()
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
        monitor.stop_monitoring()


if __name__ == "__main__":
    main()