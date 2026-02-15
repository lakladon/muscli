import os
import sys
import socket
import requests
import threading
import time
import sqlite3
from datetime import datetime
from urllib.parse import quote

# === Проверка: уже запущена ли программа ===
APP_NAME = "lakarchive_app"
app_lock = None

def check_single_instance():
    """Проверяет, запущена ли уже программа. Возвращает True если это первый запуск."""
    global app_lock
    try:
        # Пытаемся создать сокет на случайном порту
        app_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        app_lock.bind(('localhost', 0))  # bind to any available port
        return True
    except OSError:
        # Сокет уже занят - программа уже запущена
        return False

# Проверяем перед запуском
if not check_single_instance():
    print("Ошибка: LakArchive уже запущен!")
    print("Запустить вторую копию нельзя.")
    sys.exit(0)

# === PyQt5 ===
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QTabWidget, QProgressBar,
    QScrollArea, QFrame, QMessageBox, QAction, QSystemTrayIcon,
    QMenu, QScrollBar, QDialog, QHeaderView, QSlider, QStyle, QStyleOptionSlider
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QUrl
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QPalette

# === Попытка импорта мультимедиа ===
MEDIA_ENABLED = False
try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent, QMediaPlaylist
    from PyQt5.QtMultimediaWidgets import QVideoWidget
    MEDIA_ENABLED = True
except ImportError:
    print("[DEBUG] QtMultimedia not available, using fallback player")
    QMediaPlayer = None
    QMediaContent = None
    QVideoWidget = None

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
MAX_CONCURRENT_DOWNLOADS = 5  # Максимум одновременных загрузок

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
    # Упрощённый запрос - ищем везде, без ограничения коллекций
    url = "https://archive.org/advancedsearch.php"
    params = {
        'q': f'title:({quote(query)}) OR creator:({quote(query)})',
        'fl[]': ['identifier', 'title', 'creator', 'downloads'],
        'sort[]': 'downloads desc',
        'rows': per_page,
        'page': page,
        'output': 'json'
    }
    print(f"[DEBUG] fetch_page: query='{query}', page={page}")
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"[DEBUG] Status: {r.status_code}, URL: {r.url[:100]}...")
        if r.status_code == 200:
            data = r.json()
            docs = data.get('response', {}).get('docs', [])
            num_found = data.get('response', {}).get('numFound', 0)
            print(f"[DEBUG] Found: {num_found}, docs: {len(docs)}")
            return docs, num_found
        else:
            print(f"[DEBUG] Error: status {r.status_code}")
    except Exception as e:
        print(f"[DEBUG] Exception: {type(e).__name__}: {e}")
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
class DownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, str)  # download_id, percent, text
    finished_signal = pyqtSignal(int, str)  # download_id, status
    
    def __init__(self, identifier, filename, download_id, app_instance):
        super().__init__()
        self.identifier = identifier
        self.filename = filename
        self.download_id = download_id
        self.app_instance = app_instance
        self._is_running = True
    
    def run(self):
        print(f"[DEBUG] DownloadThread started: {self.filename}")
        url = f"https://archive.org/download/{self.identifier}/{quote(self.filename)}"
        clean_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in self.filename)
        path = os.path.join(DOWNLOAD_FOLDER, clean_name)
        
        try:
            print(f"[DEBUG] Downloading: {url}")
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            print(f"[DEBUG] Total size: {total_size} bytes")
            downloaded = 0
            
            with open(path, 'wb') as f:
                for chunk in r.iter_content(8192):
                    if not self._is_running:
                        print(f"[DEBUG] Download cancelled")
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = downloaded * 100 // total_size
                        self.progress_signal.emit(self.download_id, percent, f"{percent}%")
            
            if self._is_running:
                add_to_db(self.identifier, self.filename, path)
                self.finished_signal.emit(self.download_id, "Готово")
                print(f"[DEBUG] Download finished: {self.filename}")
        except Exception as e:
            print(f"[DEBUG] Download error: {type(e).__name__}: {e}")
            self.finished_signal.emit(self.download_id, f"Ошибка: {str(e)[:30]}")
        
        # Примечание: downloads_in_progress уменьшается в finish_download
        print(f"[DEBUG] DownloadThread finished: {self.filename}")
    
    def stop(self):
        """Остановка потока"""
        self._is_running = False


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
class ArchiveMusicApp(QMainWindow):
    def __init__(self):
        print("[DEBUG] ArchiveMusicApp.__init__ started")
        super().__init__()
        print("[DEBUG] super().__init__ done")
        self.setWindowTitle("LakArchive")
        self.setGeometry(100, 100, 900, 650)
        
        self.all_results = []
        self.total = 0
        self.current_page = 0
        self.query = ""
        self.downloads_in_progress = 0
        self.download_count = 0  # Счётчик для уникальных ID загрузок
        
        # Словарь для хранения виджетов загрузок: {download_id: {filename, progress_bar, status_label, frame, thread}}
        self.active_downloads = {}
        # Словарь для хранения активных потоков загрузки
        self.download_threads = {}
        # Очередь загрузок (ждёт когда освободятся слоты)
        self.download_queue = []
        
        # Настройка системного трея
        self.tray = None
        print("[DEBUG] About to setup tray")
        self.setup_tray()
        print("[DEBUG] Tray setup done")
        
        print("[DEBUG] About to setup UI")
        self.setup_ui()
        print("[DEBUG] UI setup done")
        
        print("[DEBUG] About to init DB")
        init_db()
        print("[DEBUG] DB init done")
        
        print("[DEBUG] ArchiveMusicApp.__init__ complete")
    
    def setup_tray(self):
        """Настраивает системный трей"""
        if not TRAY_ENABLED:
            return
            
        try:
            # Create a simple icon programmatically
            pixmap = QPixmap(64, 64)
            pixmap.fill(QColor(255, 255, 255))
            painter = QPainter(pixmap)
            painter.setBrush(QColor('#4CAF50'))
            painter.setPen(QColor('#388E3C'))
            painter.drawEllipse(16, 8, 32, 32)
            painter.setBrush(QColor('#388E3C'))
            painter.drawRect(40, 20, 8, 36)
            painter.drawRect(44, 56, 12, 4)
            painter.end()
            
            icon = QIcon(pixmap)
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("LakArchive")
            
            # Create tray menu
            tray_menu = QMenu()
            show_action = QAction("Показать", self)
            show_action.triggered.connect(self.showNormal)
            tray_menu.addAction(show_action)
            
            quit_action = QAction("Выход", self)
            quit_action.triggered.connect(self.quit_app)
            tray_menu.addAction(quit_action)
            
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        except Exception as e:
            print(f"Не удалось создать трей: {e}")
            self.tray = None
    
    def tray_activated(self, reason):
        """Handle tray icon click"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self.activateWindow()
    
    def quit_app(self):
        """Выход из приложения"""
        if self.tray:
            self.tray.hide()
        QApplication.quit()
    
    def closeEvent(self, event):
        """Перехватываем закрытие окна - скрываем вместо закрытия"""
        event.ignore()
        self.hide()
    
    def setup_ui(self):
        # Создаём главное меню
        menubar = self.menuBar()
        
        # === Файл ===
        file_menu = menubar.addMenu("Файл")
        
        open_folder_action = QAction("Открыть папку загрузок", self)
        open_folder_action.triggered.connect(self.open_download_folder)
        file_menu.addAction(open_folder_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self.quit_app)
        file_menu.addAction(exit_action)
        
        # === Менеджер загрузок ===
        dl_manager_menu = menubar.addMenu("Менеджер загрузок")
        
        show_dl_manager_action = QAction("Показать панель загрузок", self)
        show_dl_manager_action.triggered.connect(self.show_download_manager)
        dl_manager_menu.addAction(show_dl_manager_action)
        
        dl_manager_menu.addSeparator()
        
        pause_all_action = QAction("Приостановить все", self)
        pause_all_action.triggered.connect(self.pause_all_downloads)
        dl_manager_menu.addAction(pause_all_action)
        
        resume_all_action = QAction("Возобновить все", self)
        resume_all_action.triggered.connect(self.resume_all_downloads)
        dl_manager_menu.addAction(resume_all_action)
        
        cancel_all_action = QAction("Отменить все", self)
        cancel_all_action.triggered.connect(self.cancel_all_downloads)
        dl_manager_menu.addAction(cancel_all_action)
        
        dl_manager_menu.addSeparator()
        
        clear_finished_action = QAction("Очистить завершённые", self)
        clear_finished_action.triggered.connect(self.clear_finished_downloads)
        dl_manager_menu.addAction(clear_finished_action)
        
        dl_manager_menu.addSeparator()
        
        settings_action = QAction("Настройки загрузок...", self)
        settings_action.triggered.connect(self.show_download_settings)
        dl_manager_menu.addAction(settings_action)
        
        # === Плеер ===
        player_menu = menubar.addMenu("Плеер")
        
        open_player_action = QAction("Открыть плеер", self)
        open_player_action.triggered.connect(self.open_player)
        player_menu.addAction(open_player_action)
        
        player_menu.addSeparator()
        
        play_action = QAction("Воспроизвести", self)
        play_action.triggered.connect(self.player_play)
        player_menu.addAction(play_action)
        
        pause_action = QAction("Пауза", self)
        pause_action.triggered.connect(self.player_pause)
        player_menu.addAction(pause_action)
        
        stop_action = QAction("Стоп", self)
        stop_action.triggered.connect(self.player_stop)
        player_menu.addAction(stop_action)
        
        player_menu.addSeparator()
        
        prev_track_action = QAction("Предыдущий трек", self)
        prev_track_action.triggered.connect(self.player_prev)
        player_menu.addAction(prev_track_action)
        
        next_track_action = QAction("Следующий трек", self)
        next_track_action.triggered.connect(self.player_next)
        player_menu.addAction(next_track_action)
        
        player_menu.addSeparator()
        
        open_file_action = QAction("Открыть файл...", self)
        open_file_action.triggered.connect(self.player_open_file)
        player_menu.addAction(open_file_action)
        
        # === Вид ===
        view_menu = menubar.addMenu("Вид")
        
        toggle_downloads_action = QAction("Показать/скрыть загрузки", self)
        toggle_downloads_action.triggered.connect(self.toggle_downloads_panel)
        view_menu.addAction(toggle_downloads_action)
        
        refresh_action = QAction("Обновить результаты", self)
        refresh_action.triggered.connect(self.refresh_results)
        view_menu.addAction(refresh_action)
        
        # === Справка ===
        help_menu = menubar.addMenu("Справка")
        
        about_action = QAction("О программе", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        # Верхняя панель - поиск
        top_frame = QFrame()
        top_layout = QHBoxLayout(top_frame)
        
        search_label = QLabel("Поиск:")
        top_layout.addWidget(search_label)
        
        self.search_entry = QLineEdit()
        self.search_entry.setFixedWidth(300)
        self.search_entry.returnPressed.connect(self.start_search)
        top_layout.addWidget(self.search_entry)
        
        self.search_btn = QPushButton("Найти")
        self.search_btn.clicked.connect(self.start_search)
        top_layout.addWidget(self.search_btn)
        
        self.more_btn = QPushButton("Ещё")
        self.more_btn.clicked.connect(self.load_more)
        self.more_btn.setEnabled(False)
        top_layout.addWidget(self.more_btn)
        
        # Статус поиска + прогресс бар
        self.status_label = QLabel("")
        top_layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.progress_bar.setFixedWidth(150)
        top_layout.addWidget(self.progress_bar)
        
        top_layout.addStretch()
        main_layout.addWidget(top_frame)
        
        # Список результатов
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["#", "Название", "Исполнитель", "Загрузки"])
        self.tree.setColumnWidth(0, 50)
        self.tree.setColumnWidth(1, 400)
        self.tree.setColumnWidth(2, 250)
        self.tree.setColumnWidth(3, 80)
        self.tree.itemDoubleClicked.connect(self.open_archive)
        main_layout.addWidget(self.tree)
        
        # Placeholder - показываем когда нет результатов
        self.placeholder = QLabel("💡 Это пустота, но вы можете решить это,\nесли напишите что-нибудь в поиске")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color: gray; font-size: 12pt;")
        main_layout.addWidget(self.placeholder)
        
        # Нижняя панель - загрузки с индивидуальными прогресс-барами
        downloads_frame = QFrame()
        downloads_frame.setFrameShape(QFrame.StyledPanel)
        downloads_layout = QVBoxLayout(downloads_frame)
        
        downloads_title = QLabel("Загрузки")
        downloads_title.setStyleSheet("font-weight: bold;")
        downloads_layout.addWidget(downloads_title)
        
        # Scroll area for downloads
        self.downloads_scroll = QScrollArea()
        self.downloads_scroll.setWidgetResizable(True)
        self.downloads_scroll.setFixedHeight(150)
        
        self.downloads_container = QWidget()
        self.downloads_layout = QVBoxLayout(self.downloads_container)
        self.downloads_layout.setAlignment(Qt.AlignTop)
        self.downloads_layout.addStretch()
        
        self.downloads_scroll.setWidget(self.downloads_container)
        downloads_layout.addWidget(self.downloads_scroll)
        
        main_layout.addWidget(downloads_frame)
    
    def add_download(self, filename):
        """Добавляет новую загрузку с индивидуальным прогресс-баром"""
        self.download_count += 1
        download_id = self.download_count
        
        # Frame для этой загрузки
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame_layout = QHBoxLayout(frame)
        frame_layout.setContentsMargins(5, 2, 5, 2)
        
        # Имя файла
        name_label = QLabel(filename[:50])
        name_label.setFixedWidth(300)
        name_label.setStyleSheet("padding: 2px;")
        frame_layout.addWidget(name_label)
        
        # Прогресс-бар
        progress = QProgressBar()
        progress.setMaximum(100)
        progress.setFixedHeight(20)
        frame_layout.addWidget(progress)
        
        # Статус
        status_label = QLabel("0%")
        status_label.setFixedWidth(80)
        frame_layout.addWidget(status_label)
        
        # Кнопка отмены (крестик)
        cancel_btn = QPushButton("✕")
        cancel_btn.setFixedWidth(30)
        cancel_btn.clicked.connect(lambda: self.cancel_download(download_id))
        frame_layout.addWidget(cancel_btn)
        
        # Сохраняем виджеты
        self.active_downloads[download_id] = {
            'filename': filename,
            'progress': progress,
            'status': status_label,
            'frame': frame,
            'cancel_btn': cancel_btn,
            'cancelled': False
        }
        
        # Insert at the beginning (before stretch)
        self.downloads_layout.insertWidget(self.downloads_layout.count() - 1, frame)
        
        return download_id
    
    def update_download_progress(self, download_id, progress_value, status_text):
        """Обновляет прогресс-бар конкретной загрузки"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            if not dl['cancelled']:
                dl['progress'].setValue(progress_value)
                dl['status'].setText(status_text)
    
    def finish_download(self, download_id, status_text):
        """Отмечает загрузку как завершённую"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['progress'].setValue(100)
            dl['status'].setText(status_text)
            if status_text == "Готово":
                dl['status'].setStyleSheet("color: green;")
            # Скрываем кнопку отмены
            if 'cancel_btn' in dl:
                dl['cancel_btn'].hide()
        
        # Уменьшаем счётчик активных загрузок
        self.downloads_in_progress -= 1
        
        # Пытаемся запустить следующую загрузку из очереди
        self.process_download_queue()
    
    def process_download_queue(self):
        """Обрабатывает очередь загрузок - запускает следующие если есть свободные слоты"""
        while self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS and self.download_queue:
            # Берем первую загрузку из очереди
            queue_item = self.download_queue.pop(0)
            
            # Проверяем тип загрузки
            if queue_item.get('type') == 'torrent':
                # Торрент-загрузка
                self.downloads_in_progress += 1
                self._start_torrent_download(
                    queue_item['identifier'],
                    queue_item['torrent_file'],
                    queue_item['selected_indices'],
                    queue_item['download_id']
                )
            else:
                # Обычная загрузка
                self.downloads_in_progress += 1
                self.start_download(
                    queue_item['identifier'],
                    queue_item['filename'],
                    queue_item['download_id']
                )
    
    def queue_download(self, identifier, filename, download_id):
        """Добавляет загрузку в очередь или запускает сразу если есть свободные слоты"""
        if self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS:
            # Есть свободный слот - запускаем сразу
            self.downloads_in_progress += 1
            self.start_download(identifier, filename, download_id)
        else:
            # Нет свободных слотов - добавляем в очередь
            self.download_queue.append({
                'identifier': identifier,
                'filename': filename,
                'download_id': download_id
            })
            # Обновляем статус на "В очереди"
            if download_id in self.active_downloads:
                self.active_downloads[download_id]['status'].setText("В очереди")
    
    def cancel_download(self, download_id):
        """Отменяет загрузку (визуально)"""
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['cancelled'] = True
            dl['status'].setText("Отменено")
            dl['status'].setStyleSheet("color: red;")
            # Скрываем кнопку отмены
            if 'cancel_btn' in dl:
                dl['cancel_btn'].hide()
    
    def clear_finished_downloads(self):
        """Удаляет завершённые загрузки из списка"""
        to_remove = []
        for dl_id, dl in self.active_downloads.items():
            if dl['status'].text() in ["Готово", "Ошибка", "Отменено"]:
                to_remove.append(dl_id)
        
        for dl_id in to_remove:
            dl = self.active_downloads.pop(dl_id)
            dl['frame'].hide()
            dl['frame'].deleteLater()
    
    def start_search(self):
        query = self.search_entry.text().strip()
        if not query:
            return
        
        self.query = query
        self.all_results = []
        self.current_page = 0
        
        # Clear tree
        self.tree.clear()
        
        self.load_more()
    
    def load_more(self):
        if len(self.all_results) >= self.total and self.current_page > 0:
            return
        
        self.current_page += 1
        self.search_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self.status_label.setText("Загрузка...")
        
        # Выполняем запрос напрямую (без потока) для диагностики
        try:
            results, total = fetch_page(self.query, self.current_page)
            self.status_label.setText(f"Загружено: {len(results)} из {total}")
            self._display_results(results, total)
        except Exception as e:
            self.status_label.setText(f"Ошибка: {str(e)}")
            self.search_btn.setEnabled(True)
    
    def _display_results(self, results, total):
        print(f"[DEBUG] _display_results called: results={len(results)}, total={total}")
        
        # Скрываем placeholder при появлении результатов
        if self.current_page == 1 and len(results) > 0:
            self.placeholder.hide()
        
        if self.current_page == 1:
            self.total = total
            self.status_label.setText(f"Найдено: {total}")
            if total == 0:
                self.status_label.setText("Ничего не найдено. Попробуйте другой запрос.")
                self.placeholder.show()
        else:
            self.status_label.setText(f"Загружено: {len(self.all_results)} / {total}")
        
        # Если результатов нет - показываем сообщение
        if len(results) == 0:
            self.status_label.setText("Ничего не найдено")
            self.placeholder.show()
        
        start_idx = len(self.all_results)
        self.all_results.extend(results)
        
        for i, item in enumerate(results):
            idx = start_idx + i + 1
            title = item.get('title', '—')[:50]
            creator = item.get('creator', '???')[:30]
            downloads = item.get('downloads', 0)
            
            tree_item = QTreeWidgetItem([str(idx), title, creator, str(downloads)])
            self.tree.addTopLevelItem(tree_item)
        
        # Update progress bar
        if self.total > 0:
            progress = (len(self.all_results) / self.total) * 100
            self.progress_bar.setValue(int(progress))
        
        self.search_btn.setEnabled(True)
        
        # Enable "More" button if there are more results
        if len(self.all_results) < self.total:
            self.more_btn.setEnabled(True)
        else:
            self.progress_bar.setValue(100)
    
    def open_archive(self, item, column):
        # Get the row index
        current_item = self.tree.currentItem()
        if not current_item:
            return
        
        idx = int(current_item.text(0)) - 1
        if 0 <= idx < len(self.all_results):
            item_data = self.all_results[idx]
            self.show_archive_files(item_data['identifier'], item_data.get('title', 'Без названия'))
    
    def show_archive_files(self, identifier, title):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Файлы: {title}")
        dialog.setGeometry(150, 150, 700, 500)
        
        layout = QVBoxLayout(dialog)
        
        # Загрузка файлов
        all_files = get_all_files(identifier)
        if not all_files:
            QMessageBox.information(dialog, "Информация", "Файлы не найдены")
            dialog.close()
            return
        
        audio_files = get_audio_files(all_files)
        torrent_files = get_torrent_files(all_files)
        
        # Notebook с вкладками
        notebook = QTabWidget()
        layout.addWidget(notebook)
        
        # Вкладка аудио
        if audio_files:
            audio_frame = QWidget()
            audio_layout = QVBoxLayout(audio_frame)
            
            audio_tree = QTableWidget()
            audio_tree.setColumnCount(4)
            audio_tree.setHorizontalHeaderLabels(["#", "Файл", "Размер", "✓"])
            audio_tree.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            audio_tree.setColumnWidth(0, 50)
            audio_tree.setColumnWidth(2, 100)
            audio_tree.setColumnWidth(3, 50)
            audio_tree.setRowCount(len(audio_files))
            audio_tree.setSelectionBehavior(QTableWidget.SelectRows)
            
            audio_checkboxes = {}
            for i, f in enumerate(audio_files):
                size = human_size(int(f.get('size', 0)))
                already = is_already_downloaded(identifier, f['name'])
                selected = "✓" if not already else ""
                
                audio_tree.setItem(i, 0, QTableWidgetItem(str(i+1)))
                audio_tree.setItem(i, 1, QTableWidgetItem(f['name']))
                audio_tree.setItem(i, 2, QTableWidgetItem(size))
                audio_tree.setItem(i, 3, QTableWidgetItem(selected))
                
                if already:
                    for col in range(4):
                        item = audio_tree.item(i, col)
                        if item:
                            item.setBackground(QColor(200, 200, 200))
                
                audio_checkboxes[i] = i
            
            audio_layout.addWidget(audio_tree)
            
            audio_btn_frame = QWidget()
            audio_btn_layout = QHBoxLayout(audio_btn_frame)
            
            def toggle_audio_select():
                all_selected = True
                for i in audio_checkboxes:
                    f = audio_files[i]
                    if is_already_downloaded(identifier, f['name']):
                        continue
                    item = audio_tree.item(audio_checkboxes[i], 3)
                    if item and item.text() != "✓":
                        all_selected = False
                        break
                
                for i in audio_checkboxes:
                    f = audio_files[i]
                    if is_already_downloaded(identifier, f['name']):
                        continue
                    item = audio_tree.item(i, 3)
                    if item:
                        item.setText("" if all_selected else "✓")
            
            def download_audio_selected():
                selected = []
                for i in audio_checkboxes:
                    item = audio_tree.item(i, 3)
                    if item and item.text() == "✓":
                        selected.append(i)
                
                if not selected:
                    QMessageBox.information(dialog, "Информация", "Выберите файлы для загрузки")
                    return
                
                for idx in selected:
                    f = audio_files[idx]
                    # Добавляем загрузку в список и получаем её ID
                    download_id = self.add_download(f['name'])
                    # Добавляем в очередь (запустится сразу если есть свободный слот)
                    self.queue_download(identifier, f['name'], download_id)
                
                # Clear selections after starting downloads
                for i in audio_checkboxes:
                    item = audio_tree.item(i, 3)
                    if item:
                        item.setText("")
            
            select_all_btn = QPushButton("Выбрать все")
            select_all_btn.clicked.connect(toggle_audio_select)
            audio_btn_layout.addWidget(select_all_btn)
            
            download_btn = QPushButton("Скачать выбранные")
            download_btn.clicked.connect(download_audio_selected)
            audio_btn_layout.addWidget(download_btn)
            
            audio_layout.addWidget(audio_btn_frame)
            notebook.addTab(audio_frame, f"Аудио ({len(audio_files)})")
        
        # Вкладка торренты
        if torrent_files:
            torrent_frame = QWidget()
            torrent_layout = QVBoxLayout(torrent_frame)
            
            torrent_tree = QTableWidget()
            torrent_tree.setColumnCount(3)
            torrent_tree.setHorizontalHeaderLabels(["#", "Файл", "Размер"])
            torrent_tree.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            torrent_tree.setColumnWidth(0, 50)
            torrent_tree.setColumnWidth(2, 100)
            torrent_tree.setRowCount(len(torrent_files))
            torrent_tree.setSelectionBehavior(QTableWidget.SelectRows)
            
            for i, f in enumerate(torrent_files):
                size = human_size(int(f.get('size', 0)))
                torrent_tree.setItem(i, 0, QTableWidgetItem(str(i+1)))
                torrent_tree.setItem(i, 1, QTableWidgetItem(f['name']))
                torrent_tree.setItem(i, 2, QTableWidgetItem(size))
            
            torrent_layout.addWidget(torrent_tree)
            
            def open_torrent():
                selected_rows = torrent_tree.selectionModel().selectedRows()
                if selected_rows:
                    idx = int(selected_rows[0].row())
                    if 0 <= idx < len(torrent_files):
                        self.show_torrent_files(identifier, torrent_files[idx], dialog)
            
            torrent_btn = QPushButton("Открыть")
            torrent_btn.clicked.connect(open_torrent)
            torrent_layout.addWidget(torrent_btn)
            
            notebook.addTab(torrent_frame, "Торренты")
        
        # Кнопка закрытия
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)
        
        dialog.exec_()
    
    def start_download(self, identifier, filename, download_id):
        """Запускает загрузку файла"""
        thread = DownloadThread(identifier, filename, download_id, self)
        thread.progress_signal.connect(self.update_download_progress, Qt.QueuedConnection)
        thread.finished_signal.connect(self.finish_download, Qt.QueuedConnection)
        
        # Сохраняем ссылку на поток
        self.download_threads[download_id] = thread
        
        thread.start()
    
    def show_torrent_files(self, identifier, torrent_file, parent_window):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Файлы в торренте: {torrent_file['name']}")
        dialog.setGeometry(200, 200, 600, 400)
        
        layout = QVBoxLayout(dialog)
        
        status_label = QLabel("Анализ торрента...")
        layout.addWidget(status_label)
        dialog.show()
        
        files, error = analyze_torrent_from_archive(identifier, torrent_file)
        
        # Clear the status label
        status_label.hide()
        
        if error:
            QMessageBox.critical(dialog, "Ошибка", error)
            dialog.close()
            return
        
        # Список файлов с чекбоксами
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["#", "Файл", "Размер", "✓"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setColumnWidth(0, 50)
        table.setColumnWidth(2, 100)
        table.setColumnWidth(3, 50)
        table.setRowCount(len(files))
        table.setSelectionBehavior(QTableWidget.SelectRows)
        
        checkboxes = {}
        for i, f in enumerate(files):
            size_mb = f['size'] / (1024**2)
            path = f['path'].split('/')[-1] if '/' in f['path'] else f['path']
            
            table.setItem(i, 0, QTableWidgetItem(str(i+1)))
            table.setItem(i, 1, QTableWidgetItem(path))
            table.setItem(i, 2, QTableWidgetItem(f"{size_mb:.1f} МБ"))
            table.setItem(i, 3, QTableWidgetItem(""))
            
            checkboxes[i] = i
        
        layout.addWidget(table)
        
        btn_frame = QWidget()
        btn_layout = QHBoxLayout(btn_frame)
        
        def toggle_select():
            all_selected = True
            for i in checkboxes:
                item = table.item(i, 3)
                if item and item.text() != "✓":
                    all_selected = False
                    break
            
            for i in checkboxes:
                item = table.item(i, 3)
                if item:
                    item.setText("" if all_selected else "✓")
        
        def download_selected():
            selected = []
            for i in checkboxes:
                item = table.item(i, 3)
                if item and item.text() == "✓":
                    selected.append(i)
            
            if not selected:
                QMessageBox.information(dialog, "Информация", "Выберите файлы для загрузки")
                return
            
            # Добавляем загрузку в список и в очередь
            download_id = self.add_download(f"Торрент: {torrent_file['name']}")
            
            # Используем queue_download для управления параллельными загрузками
            self.queue_torrent_download(identifier, torrent_file, selected, download_id)
            
            dialog.close()
    
    def queue_torrent_download(self, identifier, torrent_file, selected_indices, download_id):
        """Добавляет торрент-загрузку в очередь или запускает сразу"""
        if self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS:
            # Есть свободный слот - запускаем сразу
            self.downloads_in_progress += 1
            self._start_torrent_download(identifier, torrent_file, selected_indices, download_id)
        else:
            # Нет свободных слотов - добавляем в очередь
            self.download_queue.append({
                'type': 'torrent',
                'identifier': identifier,
                'torrent_file': torrent_file,
                'selected_indices': selected_indices,
                'download_id': download_id
            })
            # Обновляем статус на "В очереди"
            if download_id in self.active_downloads:
                self.active_downloads[download_id]['status'].setText("В очереди")
    
    def _start_torrent_download(self, identifier, torrent_file, selected_indices, download_id):
        """Запускает торрент-загрузку"""
        def progress_callback(msg):
            # This runs in a thread
            pass
        
        def finish_callback():
            self.finish_download(download_id, "Готово")
        
        download_selected_from_torrent(identifier, torrent_file, selected_indices, progress_callback, finish_callback)
    
    # === Обработчики меню ===
    
    def open_download_folder(self):
        """Открывает папку загрузок в проводнике"""
        import subprocess
        try:
            if os.name == 'nt':  # Windows
                os.startfile(DOWNLOAD_FOLDER)
            elif sys.platform == 'darwin':  # macOS
                subprocess.Popen(['open', DOWNLOAD_FOLDER])
            else:  # Linux
                subprocess.Popen(['xdg-open', DOWNLOAD_FOLDER])
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось открыть папку: {e}")
    
    def toggle_downloads_panel(self):
        """Показывает/скрывает панель загрузок"""
        if hasattr(self, 'downloads_scroll'):
            self.downloads_scroll.setVisible(not self.downloads_scroll.isVisible())
    
    def refresh_results(self):
        """Обновляет результаты поиска"""
        if self.query and self.all_results:
            self.start_search()
        else:
            QMessageBox.information(self, "Информация", "Нет результатов для обновления")
    
    def pause_all_downloads(self):
        """Приостанавливает все загрузки"""
        if not self.active_downloads:
            QMessageBox.information(self, "Информация", "Нет активных загрузок")
            return
        
        paused = 0
        for dl_id, dl in self.active_downloads.items():
            status = dl['status'].text()
            if status not in ["Готово", "Ошибка", "Отменено", "В очереди"]:
                # Останавливаем поток если есть
                if dl_id in self.download_threads:
                    thread = self.download_threads[dl_id]
                    thread.stop()
                dl['status'].setText("Приостановлено")
                dl['status'].setStyleSheet("color: orange;")
                paused += 1
        
        if paused > 0:
            QMessageBox.information(self, "Информация", f"Приостановлено загрузок: {paused}")
        else:
            QMessageBox.information(self, "Информация", "Нет активных загрузок для приостановки")
    
    def resume_all_downloads(self):
        """Возобновляет все загрузки"""
        # Показываем информацию - для полноценной реализации нужно хранить состояние загрузок
        QMessageBox.information(self, "Информация", "Функция возобновления временно недоступна")
    
    def cancel_all_downloads(self):
        """Отменяет все загрузки"""
        if not self.active_downloads:
            QMessageBox.information(self, "Информация", "Нет активных загрузок")
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            "Вы уверены, что хотите отменить все загрузки?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            cancelled = 0
            for dl_id in list(self.active_downloads.keys()):
                self.cancel_download(dl_id)
                cancelled += 1
            QMessageBox.information(self, "Информация", f"Отменено загрузок: {cancelled}")
    
    def show_download_settings(self):
        """Показывает настройки загрузок"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки загрузок")
        dialog.setFixedSize(400, 250)
        
        layout = QVBoxLayout(dialog)
        
        # Папка загрузок
        folder_layout = QHBoxLayout()
        folder_label = QLabel("Папка загрузок:")
        folder_layout.addWidget(folder_label)
        
        folder_value = QLabel(DOWNLOAD_FOLDER)
        folder_value.setStyleSheet("color: #888;")
        folder_layout.addWidget(folder_value)
        
        open_btn = QPushButton("Открыть")
        open_btn.clicked.connect(self.open_download_folder)
        folder_layout.addWidget(open_btn)
        
        layout.addLayout(folder_layout)
        
        layout.addWidget(QLabel(""))  # Отступ
        
        # Максимум одновременных загрузок
        max_layout = QHBoxLayout()
        max_label = QLabel("Максимум одновременных загрузок:")
        max_layout.addWidget(max_label)
        
        max_value = QLabel(str(MAX_CONCURRENT_DOWNLOADS))
        max_value.setStyleSheet("color: #0078d4; font-weight: bold;")
        max_layout.addWidget(max_value)
        
        max_layout.addStretch()
        layout.addLayout(max_layout)
        
        layout.addWidget(QLabel(""))  # Отступ
        
        # Статус торрентов
        torrent_layout = QHBoxLayout()
        torrent_label = QLabel("Поддержка торрентов:")
        torrent_layout.addWidget(torrent_label)
        
        torrent_status = QLabel("Включена" if TORRENT_ENABLED else "Отключена")
        torrent_status.setStyleSheet("color: green;" if TORRENT_ENABLED else "color: red;")
        torrent_layout.addWidget(torrent_status)
        
        torrent_layout.addStretch()
        layout.addLayout(torrent_layout)
        
        # Статус трея
        tray_layout = QHBoxLayout()
        tray_label = QLabel("Системный трей:")
        tray_layout.addWidget(tray_label)
        
        tray_status = QLabel("Включён" if TRAY_ENABLED else "Отключён")
        tray_status.setStyleSheet("color: green;" if TRAY_ENABLED else "color: red;")
        tray_layout.addWidget(tray_status)
        
        tray_layout.addStretch()
        layout.addLayout(tray_layout)
        
        layout.addStretch()
        
        # Кнопка закрытия
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)
        
        dialog.exec_()
    
    def show_about(self):
        """Показывает информацию о программе"""
        QMessageBox.about(
            self,
            "О программе LakArchive",
            "<h3>LakArchive</h3>"
            "<p>Версия 1.0</p>"
            "<p>Приложение для поиска и загрузки музыки с Archive.org</p>"
            "<hr>"
            "<p><b>Возможности:</b></p>"
            "<ul>"
            "<li>Поиск музыки по названию и исполнителю</li>"
            "<li>Загрузка аудио файлов (MP3, FLAC, OGG, WAV)</li>"
            "<li>Поддержка торрент-загрузок</li>"
            "<li>Системный трей</li>"
            "<li>Тёмная тема</li>"
            "</ul>"
        )
    
    # === Менеджер загрузок ===
    
    def show_download_manager(self):
        """Показывает/создаёт менеджер загрузок"""
        self.downloads_scroll.setVisible(True)
        QMessageBox.information(
            self,
            "Менеджер загрузок",
            "Менеджер загрузок показан на нижней панели.\n\n"
            "Здесь вы можете:\n"
            "- Видеть прогресс всех загрузок\n"
            "- Отменять отдельные загрузки\n"
            "- Приостанавливать и возобновлять загрузки"
        )
    
    # === Плеер ===
    
    def open_player(self):
        """Открывает окно плеера"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Аудио плеер")
        dialog.setFixedSize(500, 300)
        
        layout = QVBoxLayout(dialog)
        
        # Информация о треке
        self.player_track_label = QLabel("Трек: не выбран")
        self.player_track_label.setAlignment(Qt.AlignCenter)
        self.player_track_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        layout.addWidget(self.player_track_label)
        
        # Время
        time_layout = QHBoxLayout()
        self.player_current_time = QLabel("00:00")
        time_layout.addWidget(self.player_current_time)
        
        self.player_slider = QSlider(Qt.Horizontal)
        self.player_slider.setRange(0, 100)
        time_layout.addWidget(self.player_slider)
        
        self.player_total_time = QLabel("00:00")
        time_layout.addWidget(self.player_total_time)
        
        layout.addLayout(time_layout)
        
        # Кнопки управления
        controls_layout = QHBoxLayout()
        
        prev_btn = QPushButton("⏮")
        prev_btn.clicked.connect(self.player_prev)
        controls_layout.addWidget(prev_btn)
        
        play_btn = QPushButton("▶")
        play_btn.clicked.connect(self.player_play)
        controls_layout.addWidget(play_btn)
        
        pause_btn = QPushButton("⏸")
        pause_btn.clicked.connect(self.player_pause)
        controls_layout.addWidget(pause_btn)
        
        stop_btn = QPushButton("⏹")
        stop_btn.clicked.connect(self.player_stop)
        controls_layout.addWidget(stop_btn)
        
        next_btn = QPushButton("⏭")
        next_btn.clicked.connect(self.player_next)
        controls_layout.addWidget(next_btn)
        
        layout.addLayout(controls_layout)
        
        # Громкость
        volume_layout = QHBoxLayout()
        volume_label = QLabel("Громкость:")
        volume_layout.addWidget(volume_label)
        
        self.player_volume = QSlider(Qt.Horizontal)
        self.player_volume.setRange(0, 100)
        self.player_volume.setValue(70)
        self.player_volume.setFixedWidth(200)
        volume_layout.addWidget(self.player_volume)
        
        volume_value = QLabel("70%")
        self.player_volume.valueChanged.connect(lambda v: volume_value.setText(f"{v}%"))
        volume_layout.addWidget(volume_value)
        
        volume_layout.addStretch()
        layout.addLayout(volume_layout)
        
        layout.addStretch()
        
        # Кнопки
        btn_layout = QHBoxLayout()
        
        open_file_btn = QPushButton("Открыть файл...")
        open_file_btn.clicked.connect(self.player_open_file)
        btn_layout.addWidget(open_file_btn)
        
        open_folder_btn = QPushButton("Открыть папку")
        open_folder_btn.clicked.connect(self.open_download_folder)
        btn_layout.addWidget(open_folder_btn)
        
        btn_layout.addStretch()
        
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dialog.close)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
        
        dialog.exec_()
    
    def player_play(self):
        """Воспроизводит текущий трек"""
        QMessageBox.information(self, "Плеер", "Воспроизведение: функция в разработке.\n\nДля воспроизведения используйте внешний плеер.")
    
    def player_pause(self):
        """Ставит на паузу"""
        QMessageBox.information(self, "Плеер", "Пауза: функция в разработке.")
    
    def player_stop(self):
        """Останавливает воспроизведение"""
        QMessageBox.information(self, "Плеер", "Стоп: функция в разработке.")
    
    def player_prev(self):
        """Предыдущий трек"""
        QMessageBox.information(self, "Плеер", "Предыдущий трек: функция в разработке.")
    
    def player_next(self):
        """Следующий трек"""
        QMessageBox.information(self, "Плеер", "Следующий трек: функция в разработке.")
    
    def player_open_file(self):
        """Открывает файл для воспроизведения"""
        from PyQt5.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите аудио файл",
            DOWNLOAD_FOLDER,
            "Аудио файлы (*.mp3 *.flac *.ogg *.wav);;Все файлы (*.*)"
        )
        if file_path:
            self.player_track_label.setText(f"Трек: {os.path.basename(file_path)}")
            QMessageBox.information(
                self,
                "Плеер",
                f"Выбран файл: {os.path.basename(file_path)}\n\n"
                "Функция воспроизведения в разработке."
            )


# === Тёмная тема ===
DARK_STYLESHEET = """
/* Основные настройки */
QMainWindow {
    background-color: #1e1e1e;
}

QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    selection-background-color: #0078d4;
    selection-color: #ffffff;
}

/* Меню */
QMenuBar {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border-bottom: 1px solid #3d3d3d;
}

QMenuBar::item:selected {
    background-color: #0078d4;
}

QMenu {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
}

QMenu::item:selected {
    background-color: #0078d4;
}

/* Кнопки */
QPushButton {
    background-color: #3d3d3d;
    color: #e0e0e0;
    border: 1px solid #555555;
    border-radius: 4px;
    padding: 5px 15px;
    min-height: 25px;
}

QPushButton:hover {
    background-color: #4d4d4d;
    border-color: #666666;
}

QPushButton:pressed {
    background-color: #2d2d2d;
}

QPushButton:disabled {
    background-color: #2d2d2d;
    color: #666666;
    border-color: #3d3d3d;
}

/* Поля ввода */
QLineEdit {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    border-radius: 4px;
    padding: 5px;
    selection-background-color: #0078d4;
}

QLineEdit:focus {
    border-color: #0078d4;
}

/* Метки */
QLabel {
    background-color: transparent;
    color: #e0e0e0;
}

/* Древовидный виджет (результаты поиска) */
QTreeWidget {
    background-color: #252526;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    alternate-background-color: #2d2d30;
    show-decoration-selected: 1;
}

QTreeWidget::item {
    padding: 5px;
}

QTreeWidget::item:selected {
    background-color: #0078d4;
    color: #ffffff;
}

QTreeWidget::item:hover {
    background-color: #2a2d2e;
}

QTreeWidget::header {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    padding: 5px;
    font-weight: bold;
}

/* Таблицы */
QTableWidget {
    background-color: #252526;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    alternate-background-color: #2d2d30;
    gridline-color: #3d3d3d;
}

QTableWidget::item {
    padding: 5px;
}

QTableWidget::item:selected {
    background-color: #0078d4;
    color: #ffffff;
}

QHeaderView::section {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    padding: 5px;
    font-weight: bold;
}

/* Вкладки */
QTabWidget::pane {
    border: 1px solid #3d3d3d;
    background-color: #1e1e1e;
}

QTabBar::tab {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    border-bottom: none;
    padding: 8px 20px;
    margin-right: 2px;
}

QTabBar::tab:selected {
    background-color: #0078d4;
    color: #ffffff;
}

QTabBar::tab:hover:!selected {
    background-color: #3d3d3d;
}

/* Прогресс-бар */
QProgressBar {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    border-radius: 4px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 3px;
}

/* Рамки */
QFrame {
    background-color: #1e1e1e;
}

QFrame[frameShape="4"], QFrame[frameShape="5"] {
    /* StyledPanel */
    background-color: #252526;
    border: 1px solid #3d3d3d;
    border-radius: 4px;
}

/* Области прокрутки */
QScrollArea {
    background-color: #1e1e1e;
    border: none;
}

QScrollBar:vertical {
    background-color: #2d2d2d;
    width: 12px;
    border: none;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background-color: #555555;
    min-height: 20px;
    border-radius: 6px;
}

QScrollBar::handle:vertical:hover {
    background-color: #666666;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    background-color: #2d2d2d;
    height: 12px;
    border: none;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background-color: #555555;
    min-width: 20px;
    border-radius: 6px;
}

QScrollBar::handle:horizontal:hover {
    background-color: #666666;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* Диалоговые окна */
QDialog {
    background-color: #1e1e1e;
}

/* Скроллбар в таблице */
QTableWidget QScrollBar:vertical, QTreeWidget QScrollBar:vertical {
    background-color: #2d2d2d;
}

QTableWidget QScrollBar::handle:vertical, QTreeWidget QScrollBar::handle:vertical {
    background-color: #555555;
}

/* Системный трей */
QSystemTrayIcon {
    background-color: #1e1e1e;
}
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern style
    
    # Применяем тёмную тему
    app.setStyleSheet(DARK_STYLESHEET)
    
    # Настройка палитры для дополнительной совместимости
    palette = app.palette()
    palette.setColor(palette.Window, QColor(30, 30, 30))
    palette.setColor(palette.WindowText, QColor(224, 224, 224))
    palette.setColor(palette.Base, QColor(45, 45, 45))
    palette.setColor(palette.AlternateBase, QColor(45, 45, 48))
    palette.setColor(palette.ToolTipBase, QColor(45, 45, 45))
    palette.setColor(palette.ToolTipText, QColor(224, 224, 224))
    palette.setColor(palette.Text, QColor(224, 224, 224))
    palette.setColor(palette.Button, QColor(61, 61, 61))
    palette.setColor(palette.ButtonText, QColor(224, 224, 224))
    palette.setColor(palette.BrightText, QColor(255, 255, 255))
    palette.setColor(palette.Highlight, QColor(0, 120, 212))
    palette.setColor(palette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)
    
    window = ArchiveMusicApp()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
