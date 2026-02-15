import os
import sys
import requests
import threading
import time
import sqlite3
from datetime import datetime
from urllib.parse import quote
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from urllib.parse import quote

# === Попытка импорта libtorrent (только для анализа метаданных) ===
TORRENT_ENABLED = False
try:
    import libtorrent as lt
    TORRENT_ENABLED = True
except ImportError:
    pass

# === Настройки ===
DOWNLOAD_FOLDER = os.path.expanduser("~/Music/free_archive")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
DB_PATH = os.path.join(DOWNLOAD_FOLDER, "archive_downloads.db")
RESULTS_PER_PAGE = 20

# === База данных ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            local_path TEXT NOT NULL,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_already_downloaded(archive_id, filename):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT local_path FROM downloads WHERE archive_id = ? AND filename = ?",
        (archive_id, filename)
    )
    row = cursor.fetchone()
    conn.close()
    if row and os.path.exists(row[0]):
        return row[0]
    return None

def add_to_db(archive_id, filename, local_path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO downloads (archive_id, filename, local_path) VALUES (?, ?, ?)",
        (archive_id, filename, local_path)
    )
    conn.commit()
    conn.close()

# === Вспомогательные функции ===
def human_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

# === Работа с Archive.org ===
def fetch_page(query, page, per_page=RESULTS_PER_PAGE):
    url = "https://archive.org/advancedsearch.php"
    params = {
        'q': f'(collection:(etree OR audio_music OR opensource_audio OR opensource_movies)) AND (title:({quote(query)}) OR creator:({quote(query)}))',
        'fl[]': ['identifier', 'title', 'creator', 'downloads'],
        'sort[]': 'downloads desc',
        'rows': per_page,
        'page': page,
        'output': 'json'
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get('response', {}).get('docs', []), data.get('response', {}).get('numFound', 0)
    except:
        pass
    return [], 0

def get_all_files(identifier):
    """Получает ВСЕ файлы из архива"""
    try:
        data = requests.get(f"https://archive.org/metadata/{identifier}", timeout=10).json()
        return data.get('files', [])
    except:
        return []

def get_audio_files(all_files):
    return [
        f for f in all_files
        if f.get('format') in ['VBR MP3', 'MP3', 'FLAC', 'Ogg Vorbis', 'WAVE']
        and f.get('source') == 'original'
        and f.get('name')
    ]

def get_torrent_files(all_files):
    return [
        f for f in all_files
        if f.get('name', '').endswith('.torrent')
    ]

# === Анализ торрента из архива ===
def analyze_torrent_from_archive(identifier, torrent_file):
    """Скачивает .torrent и возвращает список файлов внутри"""
    if not TORRENT_ENABLED:
        return None, "libtorrent не установлен"

    url = f"https://archive.org/download/{identifier}/{torrent_file['name']}"
    try:
        temp_path = os.path.join(DOWNLOAD_FOLDER, f".temp_{torrent_file['name']}")
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(temp_path, 'wb') as f:
            f.write(r.content)

        info = lt.torrent_info(temp_path)
        files = []
        for i, f in enumerate(info.files()):
            files.append({
                'index': i,
                'path': f.path,
                'size': f.size
            })
        
        os.remove(temp_path)
        return files, None
    except Exception as e:
        return None, str(e)

# === Загрузка выбранных файлов из торрента ===
def download_selected_from_torrent(identifier, torrent_file, selected_indices, progress_callback, finish_callback):
    """Скачивает .torrent, затем выбранные файлы через libtorrent"""
    def _download():
        if not TORRENT_ENABLED:
            progress_callback("❌ libtorrent не установлен")
            finish_callback()
            return

        url = f"https://archive.org/download/{identifier}/{torrent_file['name']}"
        torrent_path = os.path.join(DOWNLOAD_FOLDER, torrent_file['name'])
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(torrent_path, 'wb') as f:
                f.write(r.content)
        except Exception as e:
            progress_callback(f"❌ Не удалось скачать .torrent: {e}")
            finish_callback()
            return

        try:
            ses = lt.session()
            ses.listen_on(6881, 6891)
            info = lt.torrent_info(torrent_path)
            handle = ses.add_torrent({'ti': info, 'save_path': DOWNLOAD_FOLDER})

            priorities = [0] * info.num_files()
            for idx in selected_indices:
                if 0 <= idx < len(priorities):
                    priorities[idx] = 4
            handle.prioritize_files(priorities)
            handle.resume()

            progress_callback(f"📥 Загрузка {len(selected_indices)} файлов...")
            while not handle.is_seed():
                s = handle.status()
                progress_callback(f"Прогресс: {s.progress * 100:.1f}% | Скорость: {s.download_rate / 1000:.1f} kB/s")
                time.sleep(1)
                if s.progress >= 1.0:
                    break
            progress_callback("✅ Готово!")
        except Exception as e:
            progress_callback(f"❌ Ошибка: {e}")

        finish_callback()

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()

# === Простая загрузка аудио ===
def download_file_simple(identifier, filename, progress_callback, finish_callback):
    def _download():
        url = f"https://archive.org/download/{identifier}/{filename}"
        clean_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)
        path = os.path.join(DOWNLOAD_FOLDER, clean_name)
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(path, 'wb') as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress_callback(f"Загрузка: {downloaded}/{total_size} ({downloaded*100//total_size}%)")
            add_to_db(identifier, filename, path)
            progress_callback(f"✅ Сохранено: {path}")
        except Exception as e:
            progress_callback(f"❌ Ошибка: {e}")
        finish_callback()
    
    threading.Thread(target=_download, daemon=True).start()


# === GUI Приложение ===
class ArchiveMusicApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Internet Archive Music Downloader")
        self.root.geometry("900x600")
        
        self.all_results = []
        self.total = 0
        self.current_page = 0
        self.query = ""
        self.downloads_in_progress = 0
        
        self.setup_ui()
        init_db()
    
    def setup_ui(self):
        # Верхняя панель - поиск
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(top_frame, text="Поиск:").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(top_frame, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", lambda e: self.start_search())
        
        self.search_btn = ttk.Button(top_frame, text="Найти", command=self.start_search)
        self.search_btn.pack(side=tk.LEFT)
        
        self.more_btn = ttk.Button(top_frame, text="Ещё", command=self.load_more, state=tk.DISABLED)
        self.more_btn.pack(side=tk.LEFT, padx=5)
        
        # Статус поиска + прогресс бар
        self.status_frame = ttk.Frame(top_frame)
        self.status_frame.pack(side=tk.LEFT, padx=10)
        
        self.status_label = ttk.Label(self.status_frame, text="")
        self.status_label.pack(side=tk.LEFT)
        
        self.progress_bar = ttk.Progressbar(self.status_frame, mode='determinate', length=150, maximum=100)
        self.progress_bar.pack(side=tk.LEFT, padx=10)
        
        # Список результатов
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)
        
        self.tree = ttk.Treeview(list_frame, columns=("title", "creator", "downloads"), show="tree headings")
        self.tree.heading("#0", text="#")
        self.tree.heading("title", text="Название")
        self.tree.heading("creator", text="Исполнитель")
        self.tree.heading("downloads", text="Загрузки")
        
        self.tree.column("#0", width=50)
        self.tree.column("title", width=400)
        self.tree.column("creator", width=250)
        self.tree.column("downloads", width=80)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=self._on_scroll)
        self.last_scroll_pos = 0
        self.loading_more = False
        
        self.tree.bind("<Double-1>", lambda e: self.open_archive())
        
        # Нижняя панель - загрузки
        bottom_frame = ttk.LabelFrame(self.root, text="Загрузки")
        bottom_frame.pack(fill=tk.X, padx=10, pady=10)
        
        # Прогресс бар для загрузок
        self.download_progress = ttk.Progressbar(bottom_frame, mode='determinate', length=200)
        self.download_progress.pack(fill=tk.X, padx=5, pady=(5,0))
        
        self.download_log = scrolledtext.ScrolledText(bottom_frame, height=6, state=tk.DISABLED)
        self.download_log.pack(fill=tk.X, padx=5, pady=5)
    
    def log(self, message):
        self.download_log.config(state=tk.NORMAL)
        self.download_log.insert(tk.END, message + "\n")
        self.download_log.see(tk.END)
        self.download_log.config(state=tk.DISABLED)
    
    def update_download_progress(self, current, total):
        """Обновляет прогресс-бар загрузок"""
        if total > 0:
            self.download_progress['value'] = (current / total) * 100
        else:
            self.download_progress['value'] = 0
    
    def reset_download_progress(self):
        """Сбрасывает прогресс-бар загрузок"""
        self.download_progress['value'] = 0
    
    def start_search(self):
        query = self.search_entry.get().strip()
        if not query:
            return
        
        self.query = query
        self.all_results = []
        self.current_page = 0
        
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        self.load_more()
    
    def _on_scroll(self, *args):
        # Handle scrollbar events - args can be like ("moveto", "0.0") or ("scroll", "-1", "units")
        if len(args) == 2 and args[0] == "moveto":
            self.tree.yview("moveto", args[1])
        elif len(args) == 3 and args[0] == "scroll":
            self.tree.yview("scroll", args[1], args[2])
        else:
            # Fallback - just forward whatever we get
            try:
                self.tree.yview(*args)
            except:
                pass
        
        # Auto-load more when near bottom
        if self.current_page > 0 and len(self.all_results) < self.total:
            scroll_pos = self.tree.yview()
            if scroll_pos[1] > 0.95:  # Near bottom (95%)
                self.load_more()
    
    def load_more(self):
        if len(self.all_results) >= self.total and self.current_page > 0:
            return
        
        self.current_page += 1
        self.search_btn.config(state=tk.DISABLED)
        self.more_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Загрузка...")
        
        def _fetch():
            results, total = fetch_page(self.query, self.current_page)
            self.root.after(0, lambda: self._display_results(results, total))
        
        threading.Thread(target=_fetch, daemon=True).start()
    
    def _display_results(self, results, total):
        if self.current_page == 1:
            self.total = total
            self.status_label.config(text=f"Найдено: {total}")
        else:
            self.status_label.config(text=f"Загружено: {len(self.all_results)} / {total}")
        
        start_idx = len(self.all_results)
        self.all_results.extend(results)
        
        for i, item in enumerate(results):
            idx = start_idx + i + 1
            title = item.get('title', '—')[:50]
            creator = item.get('creator', '???')[:30]
            downloads = item.get('downloads', 0)
            self.tree.insert("", tk.END, text=str(idx), values=(title, creator, downloads))
        
        # Update progress bar
        if self.total > 0:
            progress = (len(self.all_results) / self.total) * 100
            self.progress_bar['value'] = progress
        
        self.search_btn.config(state=tk.NORMAL)
        
        # Enable "More" button if there are more results
        if len(self.all_results) < self.total:
            self.more_btn.config(state=tk.NORMAL)
        else:
            self.progress_bar['value'] = 100
    
    def open_archive(self):
        selection = self.tree.selection()
        if not selection:
            return
        
        idx = int(self.tree.item(selection[0])["text"]) - 1
        if 0 <= idx < len(self.all_results):
            item = self.all_results[idx]
            self.show_archive_files(item['identifier'], item.get('title', 'Без названия'))
    
    def show_archive_files(self, identifier, title):
        files_window = tk.Toplevel(self.root)
        files_window.title(f"Файлы: {title}")
        files_window.geometry("700x500")
        
        all_files = get_all_files(identifier)
        if not all_files:
            messagebox.showinfo("Информация", "Файлы не найдены")
            files_window.destroy()
            return
        
        audio_files = get_audio_files(all_files)
        torrent_files = get_torrent_files(all_files)
        
        # Notebook с вкладками
        notebook = ttk.Notebook(files_window)
        notebook.pack(fill=tk.BOTH, expand=True)
        
        # Вкладка аудио
        if audio_files:
            audio_frame = ttk.Frame(notebook)
            notebook.add(audio_frame, text=f"Аудио ({len(audio_files)})")
            
            audio_tree = ttk.Treeview(audio_frame, columns=("name", "size", "selected"), show="tree headings")
            audio_tree.heading("#0", text="#")
            audio_tree.heading("name", text="Файл")
            audio_tree.heading("size", text="Размер")
            audio_tree.heading("selected", text="✓")
            audio_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            
            audio_checkboxes = {}
            for i, f in enumerate(audio_files):
                size = human_size(int(f.get('size', 0)))
                already = is_already_downloaded(identifier, f['name'])
                selected = "✓" if not already else ""
                item_id = audio_tree.insert("", tk.END, text=str(i+1), values=(f['name'], size, selected))
                audio_checkboxes[item_id] = i
            
            def on_audio_click(event):
                item_id = audio_tree.identify('item', event.x, event.y)
                if item_id and item_id in audio_checkboxes:
                    idx = audio_checkboxes[item_id]
                    f = audio_files[idx]
                    if is_already_downloaded(identifier, f['name']):
                        return
                    current = audio_tree.item(item_id)["values"][2]
                    audio_tree.item(item_id, values=(f['name'], human_size(int(f.get('size', 0))), "✓" if not current else ""))
            
            audio_tree.bind("<Button-1>", on_audio_click)
            
            audio_btn_frame = ttk.Frame(audio_frame)
            audio_btn_frame.pack(pady=5)
            
            def toggle_audio_select():
                all_selected = all(
                    audio_tree.item(item_id)["values"][2] == "✓" or is_already_downloaded(identifier, audio_files[audio_checkboxes[item_id]]['name'])
                    for item_id in audio_checkboxes
                )
                for item_id in audio_checkboxes:
                    idx = audio_checkboxes[item_id]
                    f = audio_files[idx]
                    if is_already_downloaded(identifier, f['name']):
                        continue
                    current = audio_tree.item(item_id)["values"][2]
                    audio_tree.item(item_id, values=(f['name'], human_size(int(f.get('size', 0))), "" if all_selected else "✓"))
            
            def download_audio_selected():
                selected = []
                for item_id, idx in audio_checkboxes.items():
                    if audio_tree.item(item_id)["values"][2] == "✓":
                        selected.append(idx)
                
                if not selected:
                    messagebox.showinfo("Информация", "Выберите файлы для загрузки")
                    return
                
                for idx in selected:
                    f = audio_files[idx]
                    self.log(f"Загрузка: {f['name']}")
                    self.downloads_in_progress += 1
                    download_file_simple(
                        identifier, f['name'],
                        lambda m: self.log(m),
                        lambda i=idx: self._finish_audio_download(audio_tree, audio_checkboxes, identifier, audio_files)
                    )
                
                # Clear selections after starting downloads
                for item_id in audio_checkboxes:
                    idx = audio_checkboxes[item_id]
                    f = audio_files[idx]
                    audio_tree.item(item_id, values=(f['name'], human_size(int(f.get('size', 0))), ""))
            
            ttk.Button(audio_btn_frame, text="Выбрать все", command=toggle_audio_select).pack(side=tk.LEFT, padx=5)
            ttk.Button(audio_btn_frame, text="Скачать выбранные", command=download_audio_selected).pack(side=tk.LEFT, padx=5)
        
        # Вкладка торренты
        if torrent_files:
            torrent_frame = ttk.Frame(notebook)
            notebook.add(torrent_frame, text="Торренты")
            
            torrent_tree = ttk.Treeview(torrent_frame, columns=("name", "size"), show="tree headings")
            torrent_tree.heading("#0", text="#")
            torrent_tree.heading("name", text="Файл")
            torrent_tree.heading("size", text="Размер")
            torrent_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            
            for i, f in enumerate(torrent_files):
                size = human_size(int(f.get('size', 0)))
                torrent_tree.insert("", tk.END, text=str(i+1), values=(f['name'], size))
            
            def open_torrent():
                sel = torrent_tree.selection()
                if sel:
                    idx = int(torrent_tree.item(sel[0])["text"]) - 1
                    if 0 <= idx < len(torrent_files):
                        self.show_torrent_files(identifier, torrent_files[idx], files_window)
            
            ttk.Button(torrent_frame, text="Открыть", command=open_torrent).pack(pady=5)
    
    def show_torrent_files(self, identifier, torrent_file, parent_window):
        files_window = tk.Toplevel(self.root)
        files_window.title(f"Файлы в торренте: {torrent_file['name']}")
        files_window.geometry("600x400")
        
        ttk.Label(files_window, text="Анализ торрента...").pack(pady=20)
        files_window.update()
        
        files, error = analyze_torrent_from_archive(identifier, torrent_file)
        
        for widget in files_window.winfo_children():
            widget.destroy()
        
        if error:
            messagebox.showerror("Ошибка", error)
            files_window.destroy()
            return
        
        # Список файлов с чекбоксами
        frame = ttk.Frame(files_window)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        tree = ttk.Treeview(frame, columns=("path", "size", "selected"), show="tree headings")
        tree.heading("#0", text="#")
        tree.heading("path", text="Файл")
        tree.heading("size", text="Размер")
        tree.heading("selected", text="✓")
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.configure(yscrollcommand=scrollbar.set)
        
        checkboxes = {}
        for i, f in enumerate(files):
            size_mb = f['size'] / (1024**2)
            path = f['path'].split('/')[-1] if '/' in f['path'] else f['path']
            item_id = tree.insert("", tk.END, text=str(i+1), values=(path, f"{size_mb:.1f} МБ", ""))
            checkboxes[item_id] = i
        
        def on_torrent_click(event):
            item_id = tree.identify('item', event.x, event.y)
            if item_id and item_id in checkboxes:
                current = tree.item(item_id)["values"][2]
                idx = checkboxes[item_id]
                f = files[idx]
                size_mb = f['size'] / (1024**2)
                path = f['path'].split('/')[-1] if '/' in f['path'] else f['path']
                tree.item(item_id, values=(path, f"{size_mb:.1f} МБ", "✓" if not current else ""))
        
        tree.bind("<Button-1>", on_torrent_click)
        
        btn_frame = ttk.Frame(files_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def toggle_select():
            all_selected = all(tree.item(item_id)["values"][2] == "✓" for item_id in checkboxes)
            for item_id in checkboxes:
                idx = checkboxes[item_id]
                f = files[idx]
                size_mb = f['size'] / (1024**2)
                path = f['path'].split('/')[-1] if '/' in f['path'] else f['path']
                tree.item(item_id, values=(path, f"{size_mb:.1f} МБ", "" if all_selected else "✓"))
        
        def download_selected():
            selected = []
            for item_id, idx in checkboxes.items():
                if tree.item(item_id)["values"][2] == "✓":
                    selected.append(idx)
            
            if not selected:
                messagebox.showinfo("Информация", "Выберите файлы для загрузки")
                return
            
            self.log(f"Загрузка {len(selected)} файлов из торрента...")
            self.downloads_in_progress += 1
            download_selected_from_torrent(
                identifier, torrent_file, selected,
                lambda m: self.log(m),
                lambda: self._finish_torrent_download()
            )
            files_window.destroy()
        
        ttk.Button(btn_frame, text="Выбрать все", command=toggle_select).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Скачать выбранные", command=download_selected).pack(side=tk.LEFT, padx=5)
    
    def _finish_audio_download(self, tree, checkboxes, identifier, audio_files):
        self.downloads_in_progress -= 1
        # Refresh the list to show downloaded status
        for item_id in checkboxes:
            idx = checkboxes[item_id]
            f = audio_files[idx]
            path = is_already_downloaded(identifier, f['name'])
            if path:
                tree.item(item_id, values=(f['name'], human_size(int(f.get('size', 0))), "✓ Скачано"))
    
    def _finish_download(self, tree, idx, identifier, audio_files):
        self.downloads_in_progress -= 1
        if 0 <= idx < len(audio_files):
            f = audio_files[idx]
            path = is_already_downloaded(identifier, f['name'])
            if path:
                for item in tree.get_children():
                    if tree.item(item)["text"] == str(idx + 1):
                        tree.item(item, values=(f['name'], human_size(int(f.get('size', 0))), "✓ Скачано"))
                        break
    
    def _finish_torrent_download(self):
        self.downloads_in_progress -= 1


def main():
    root = tk.Tk()
    app = ArchiveMusicApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
