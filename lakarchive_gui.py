import os
import sys
import requests
import threading
import time
import sqlite3
from datetime import datetime
from urllib.parse import quote
import tkinter as tk
from tkinter import ttk, messagebox

# === Попытка импорта для системного трея ===
TRAY_ENABLED = False
try:
    from PIL import Image, ImageDraw
    import pystray
    TRAY_ENABLED = True
except ImportError:
    pass

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
def download_file_simple(identifier, filename, download_id, app_instance):
    """Загрузка файла с обновлением прогресс-бара"""
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
                        percent = downloaded * 100 // total_size
                        app_instance.root.after(0, lambda: app_instance.update_download_progress(
                            download_id, percent, f"{downloaded*100//total_size}%"
                        ))
            add_to_db(identifier, filename, path)
            app_instance.root.after(0, lambda: app_instance.finish_download(download_id, "Готово"))
        except Exception as e:
            app_instance.root.after(0, lambda: app_instance.finish_download(download_id, "Ошибка"))
        
        app_instance.root.after(0, lambda: setattr(app_instance, 'downloads_in_progress', app_instance.downloads_in_progress - 1))
    
    threading.Thread(target=_download, daemon=True).start()


# === Создание иконки для трея ===
def create_tray_icon():
    """Создаёт простую иконку для трея"""
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), color='white')
    dc = ImageDraw.Draw(image)
    # Рисуем простой музыкальный символ (нота)
    dc.ellipse([16, 8, 48, 40], fill='#4CAF50', outline='#388E3C')
    dc.rectangle([40, 20, 48, 56], fill='#388E3C')
    dc.rectangle([44, 56, 56, 60], fill='#388E3C')
    return image


# === GUI Приложение ===
class ArchiveMusicApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LakArchive")
        self.root.geometry("900x650")
        
        self.all_results = []
        self.total = 0
        self.current_page = 0
        self.query = ""
        self.downloads_in_progress = 0
        self.download_count = 0  # Счётчик для уникальных ID загрузок
        
        # Словарь для хранения виджетов загрузок: {download_id: {filename, progress_bar, status_label, frame}}
        self.active_downloads = {}
        
        # Настройка системного трея (только для Windows с pystray)
        self.tray = None
        self.setup_tray()
        
        # Обработка закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.setup_ui()
        init_db()
    
    def setup_tray(self):
        """Настраивает системный трей"""
        if not TRAY_ENABLED:
            return
            
        try:
            image = create_tray_icon()
            self.tray = pystray.Icon("lakarchive", image, "LakArchive", self.create_tray_menu())
            # Запускаем трей в отдельном потоке
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            print(f"Не удалось создать трей: {e}")
            self.tray = None
    
    def create_tray_menu(self):
        """Создаёт меню трея"""
        menu = pystray.Menu(
            pystray.MenuItem("Показать", self.show_window, default=True),
            pystray.MenuItem("Выход", self.quit_app)
        )
        return menu
    
    def show_window(self, icon=None, item=None):
        """Показывает окно приложения"""
        self.root.after(0, self._show_window)
    
    def _show_window(self):
        """Внутренний метод показа окна"""
        self.root.deiconify()
        self.root.state('normal')
        self.root.lift()
        self.root.focus()
    
    def hide_window(self):
        """Скрывает окно в трей"""
        self.root.withdraw()
    
    def quit_app(self, icon=None, item=None):
        """Выход из приложения"""
        # Сначала останавливаем трей
        if self.tray:
            try:
                self.tray.stop()
            except:
                pass
        # Используем after для выхода из основного потока
        self.root.after(100, self._force_quit)
    
    def _force_quit(self):
        """Принудительный выход"""
        try:
            os._exit(0)
        except:
            pass
    
    def on_closing(self):
        """Обработка закрытия окна - сворачиваем в трей если трей активен"""
        if self.tray is not None and TRAY_ENABLED:
            self.hide_window()
        else:
            self.quit_app()
    
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
        
        # Placeholder - показываем когда нет результатов
        self.placeholder = tk.Label(
            list_frame,
            text="💡 Это пустота, но вы можете решить это,\nесли напишите что-нибудь в поиске",
            font=("Arial", 12),
            bg="#f0f0f0",
            fg="gray"
        )
        self.placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=self._on_scroll)
        self.last_scroll_pos = 0
        self.loading_more = False
        
        self.tree.bind("<Double-1>", lambda e: self.open_archive())
        
        # Нижняя панель - загрузки с индивидуальными прогресс-барами
        bottom_frame = ttk.LabelFrame(self.root, text="Загрузки")
        bottom_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Canvas с прокруткой для списка загрузок
        self.downloads_canvas = tk.Canvas(bottom_frame, height=150)
        self.downloads_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.downloads_scrollbar = ttk.Scrollbar(bottom_frame, orient=tk.VERTICAL, command=self.downloads_canvas.yview)
        self.downloads_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.downloads_canvas.configure(yscrollcommand=self.downloads_scrollbar.set)
        
        # Frame для размещения загрузок внутри canvas
        self.downloads_container = ttk.Frame(self.downloads_canvas)
        self.downloads_canvas.create_window((0, 0), window=self.downloads_container, anchor=tk.NW)
        
        self.downloads_container.bind("<Configure>", lambda e: self.downloads_canvas.configure(scrollregion=self.downloads_canvas.bbox("all")))
    
    def add_download(self, filename):
        """Добавляет новую загрузку с индивидуальным прогресс-баром"""
        self.download_count += 1
        download_id = self.download_count
        
        # Frame для этой загрузки
        frame = ttk.Frame(self.downloads_container)
        frame.pack(fill=tk.X, padx=5, pady=2)
        
        # Имя файла
        name_label = ttk.Label(frame, text=filename[:50], width=50, anchor=tk.W)
        name_label.pack(side=tk.LEFT, padx=(0, 5))
        
        # Прогресс-бар
        progress = ttk.Progressbar(frame, mode='determinate', length=300)
        progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # Статус
        status_label = ttk.Label(frame, text="0%", width=10)
        status_label.pack(side=tk.LEFT, padx=5)
        
        # Кнопка отмены (крестик)
        cancel_btn = ttk.Button(frame, text="✕", width=3, command=lambda: self.cancel_download(download_id))
        cancel_btn.pack(side=tk.LEFT, padx=2)
        
        # Сохраняем виджеты
        self.active_downloads[download_id] = {
            'filename': filename,
            'progress': progress,
            'status': status_label,
            'frame': frame,
            'cancel_btn': cancel_btn,
            'cancelled': False
        }
        
        # Прокрутка к новой загрузке
        self.downloads_canvas.update_idletasks()
        self.downloads_canvas.yview_moveto(1)
        
        return download_id
    
    def update_download_progress(self, download_id, progress_value, status_text):
        """Обновляет прогресс-бар конкретной загрузки"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            if not dl['cancelled']:
                dl['progress']['value'] = progress_value
                dl['status'].config(text=status_text)
    
    def finish_download(self, download_id, status_text):
        """Отмечает загрузку как завершённую"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['progress']['value'] = 100
            dl['status'].config(text=status_text, foreground="green")
            # Скрываем кнопку отмены
            if 'cancel_btn' in dl:
                dl['cancel_btn'].pack_forget()
    
    def cancel_download(self, download_id):
        """Отменяет загрузку (визуально)"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['cancelled'] = True
            dl['status'].config(text="Отменено", foreground="red")
            # Скрываем кнопку отмены
            if 'cancel_btn' in dl:
                dl['cancel_btn'].pack_forget()
    
    def clear_finished_downloads(self):
        """Удаляет завершённые загрузки из списка"""
        to_remove = []
        for dl_id, dl in self.active_downloads.items():
            if dl['status'].cget("text") in ["Готово", "Ошибка", "Отменено"]:
                to_remove.append(dl_id)
        
        for dl_id in to_remove:
            dl = self.active_downloads.pop(dl_id)
            dl['frame'].destroy()
    
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
        # Скрываем placeholder при появлении результатов
        if self.current_page == 1 and len(results) > 0:
            self.placeholder.place_forget()
        
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
                    # Добавляем загрузку в список и получаем её ID
                    download_id = self.add_download(f['name'])
                    self.downloads_in_progress += 1
                    # Запускаем загрузку
                    download_file_simple(identifier, f['name'], download_id, self)
                
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
            
            # Добавляем загрузку в список
            download_id = self.add_download(f"Торрент: {torrent_file['name']}")
            self.downloads_in_progress += 1
            
            def progress_callback(msg):
                self.root.after(0, lambda: self.update_download_progress(download_id, 50, msg[:20]))
            
            def finish_callback():
                self.root.after(0, lambda: self.finish_download(download_id, "Готово"))
                self.root.after(0, lambda: setattr(self, 'downloads_in_progress', self.downloads_in_progress - 1))
            
            download_selected_from_torrent(identifier, torrent_file, selected, progress_callback, finish_callback)
            files_window.destroy()
        
        ttk.Button(btn_frame, text="Выбрать все", command=toggle_select).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Скачать выбранные", command=download_selected).pack(side=tk.LEFT, padx=5)


def main():
    root = tk.Tk()
    app = ArchiveMusicApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
