import os
import sys
import socket
import requests
import threading
import time
import sqlite3
import logging
from datetime import datetime
from urllib.parse import quote

# === Check: is the program already running ===
APP_NAME = "lakarchive_app"
app_lock = None

def check_single_instance():
    """Check if the program is already running. Returns True if it's the first launch."""
    global app_lock
    try:
        app_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        app_lock.bind(('localhost', 0))
        return True
    except OSError:
        return False

if not check_single_instance():
    print("Error: LakArchive is already running!")
    print("Cannot launch a second copy.")
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

# === Try import for system tray ===
TRAY_ENABLED = False
try:
    from PIL import Image, ImageDraw
    import pystray
    TRAY_ENABLED = True
except ImportError:
    pass

# === Try import libtorrent (only for metadata analysis) ===
TORRENT_ENABLED = False
try:
    import libtorrent as lt
    TORRENT_ENABLED = True
except ImportError:
    pass

# === Settings / Config ===
import json

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lakarchive_config.json")

DEFAULT_SETTINGS = {
    "download_folder": "G:\\",
    "results_per_page": 20,
    "max_concurrent_downloads": 5,
    "first_run": True
}

def load_config():
    """Load configuration from file. Returns dict with settings."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            print(f"[DEBUG] Config loaded from: {CONFIG_FILE}")
            return config
        except Exception as e:
            print(f"[DEBUG] Error loading config: {e}")
    
    print("[DEBUG] No config file found, using defaults")
    return DEFAULT_SETTINGS.copy()

def save_config(settings):
    """Save configuration to file."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
        print(f"[DEBUG] Config saved to: {CONFIG_FILE}")
        return True
    except Exception as e:
        print(f"[DEBUG] Error saving config: {e}")
        return False

def is_first_run():
    """Check if this is the first run (no config file exists)."""
    return not os.path.exists(CONFIG_FILE)

# Load settings
SETTINGS = load_config()
DOWNLOAD_FOLDER = SETTINGS.get("download_folder", DEFAULT_SETTINGS["download_folder"])
RESULTS_PER_PAGE = SETTINGS.get("results_per_page", DEFAULT_SETTINGS["results_per_page"])
MAX_CONCURRENT_DOWNLOADS = SETTINGS.get("max_concurrent_downloads", DEFAULT_SETTINGS["max_concurrent_downloads"])

# Create download folder if it doesn't exist
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
logging.info(f"DOWNLOAD_FOLDER set to: {DOWNLOAD_FOLDER}")
DB_PATH = os.path.join(DOWNLOAD_FOLDER, "archive_downloads.db")

# === Database ===
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

# === Helper functions ===
def human_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

# === Archive.org work ===
def check_server_connection():
    """Check if archive.org server is reachable. Returns True if connected."""
    try:
        r = requests.get("https://archive.org/", timeout=5)
        return r.status_code == 200
    except:
        return False

def fetch_page(query, page, per_page=RESULTS_PER_PAGE):
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

# === Analyze torrent from archive ===
def analyze_torrent_from_archive(identifier, torrent_file):
    if not TORRENT_ENABLED:
        return None, "libtorrent is not installed"

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

# === Download selected files from torrent ===
def download_selected_from_torrent(identifier, torrent_file, selected_indices, progress_callback, finish_callback):
    def _download():
        if not TORRENT_ENABLED:
            progress_callback("libtorrent is not installed")
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
            progress_callback(f"Failed to download .torrent: {e}")
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

            progress_callback(f"Downloading {len(selected_indices)} files...")
            while not handle.is_seed():
                s = handle.status()
                progress_callback(f"Progress: {s.progress * 100:.1f}% | Speed: {s.download_rate / 1000:.1f} kB/s")
                time.sleep(1)
                if s.progress >= 1.0:
                    break
            progress_callback("Done!")
        except Exception as e:
            progress_callback(f"Error: {e}")

        finish_callback()

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()

# === Simple audio download ===
class DownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, str)
    finished_signal = pyqtSignal(int, str)
    
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
                self.finished_signal.emit(self.download_id, "Done")
                print(f"[DEBUG] Download finished: {self.filename}")
        except Exception as e:
            print(f"[DEBUG] Download error: {type(e).__name__}: {e}")
            self.finished_signal.emit(self.download_id, f"Error: {str(e)[:30]}")
        
        print(f"[DEBUG] DownloadThread finished: {self.filename}")
    
    def stop(self):
        self._is_running = False


# === Search thread (async) ===
class SearchThread(QThread):
    """Async search thread to prevent UI freezing."""
    search_finished_signal = pyqtSignal(list, int)
    search_error_signal = pyqtSignal(str)
    
    def __init__(self, query, page):
        super().__init__()
        self.query = query
        self.page = page
    
    def run(self):
        try:
            results, total = fetch_page(self.query, self.page)
            self.search_finished_signal.emit(results, total)
        except Exception as e:
            self.search_error_signal.emit(str(e))


# === Create tray icon ===
def create_tray_icon():
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), color='white')
    dc = ImageDraw.Draw(image)
    dc.ellipse([16, 8, 48, 40], fill='#4CAF50', outline='#388E3C')
    dc.rectangle([40, 20, 48, 56], fill='#388E3C')
    dc.rectangle([44, 56, 56, 60], fill='#388E3C')
    return image


# === GUI Application ===
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
        self.download_count = 0
        
        self.active_downloads = {}
        self.download_threads = {}
        self.download_queue = []
        
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
        
        # Set focus to tree widget after search
        self.search_btn.clicked.connect(self.on_search_finished)
        
        print("[DEBUG] ArchiveMusicApp.__init__ complete")
    
    def on_search_finished(self):
        """Set focus to tree widget after search is done."""
        if self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.tree.setFocus()
    
    def on_tree_item_clicked(self, item, column):
        """Handle single click on tree item - prepare for keyboard navigation."""
        pass
    
    def keyPressEvent(self, event):
        """Handle keyboard navigation in results list."""
        key = event.key()
        
        # If no results, just pass to default handling
        if self.tree.topLevelItemCount() == 0:
            super().keyPressEvent(event)
            return
        
        current_row = self.tree.currentIndex().row()
        
        if key == Qt.Key_Return or key == Qt.Key_Enter:
            # Enter - open archive
            self.open_archive(None, None)
        elif key == Qt.Key_Down or key == Qt.Key_J:
            # Down arrow or J - move to next item
            if current_row < self.tree.topLevelItemCount() - 1:
                self.tree.setCurrentItem(self.tree.topLevelItem(current_row + 1))
                self.tree.scrollToItem(self.tree.topLevelItem(current_row + 1))
        elif key == Qt.Key_Up or key == Qt.Key_K:
            # Up arrow or K - move to previous item
            if current_row > 0:
                self.tree.setCurrentItem(self.tree.topLevelItem(current_row - 1))
                self.tree.scrollToItem(self.tree.topLevelItem(current_row - 1))
        elif key == Qt.Key_Home:
            # Home - move to first item
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.tree.scrollToItem(self.tree.topLevelItem(0))
        elif key == Qt.Key_End:
            # End - move to last item
            last_idx = self.tree.topLevelItemCount() - 1
            self.tree.setCurrentItem(self.tree.topLevelItem(last_idx))
            self.tree.scrollToItem(self.tree.topLevelItem(last_idx))
        elif key == Qt.Key_Space:
            # Space - load more results
            if self.more_btn.isEnabled():
                self.load_more()
        elif key == Qt.Key_Backspace or key == Qt.Key_Escape:
            # Backspace/Escape - focus back to search
            self.search_entry.setFocus()
            self.search_entry.selectAll()
        elif key == Qt.Key_F5:
            # F5 - refresh results
            self.refresh_results()
        else:
            # Pass to default handling for other keys
            super().keyPressEvent(event)
    
    def setup_tray(self):
        if not TRAY_ENABLED:
            return
            
        try:
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
            
            tray_menu = QMenu()
            show_action = QAction("Show", self)
            show_action.triggered.connect(self.showNormal)
            tray_menu.addAction(show_action)
            
            quit_action = QAction("Exit", self)
            quit_action.triggered.connect(self.quit_app)
            tray_menu.addAction(quit_action)
            
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        except Exception as e:
            print(f"Failed to create tray: {e}")
            self.tray = None
    
    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self.activateWindow()
    
    def quit_app(self):
        if self.tray:
            self.tray.hide()
        QApplication.quit()
    
    def closeEvent(self, event):
        event.ignore()
        self.hide()
    
    def setup_ui(self):
        menubar = self.menuBar()
        
        # === File ===
        file_menu = menubar.addMenu("File")
        
        open_folder_action = QAction("Open downloads folder", self)
        open_folder_action.triggered.connect(self.open_download_folder)
        file_menu.addAction(open_folder_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.quit_app)
        file_menu.addAction(exit_action)
        
        # === Download Manager ===
        dl_manager_menu = menubar.addMenu("Download Manager")
        
        show_dl_manager_action = QAction("Show downloads panel", self)
        show_dl_manager_action.triggered.connect(self.show_download_manager)
        dl_manager_menu.addAction(show_dl_manager_action)
        
        dl_manager_menu.addSeparator()
        
        pause_all_action = QAction("Pause all", self)
        pause_all_action.triggered.connect(self.pause_all_downloads)
        dl_manager_menu.addAction(pause_all_action)
        
        resume_all_action = QAction("Resume all", self)
        resume_all_action.triggered.connect(self.resume_all_downloads)
        dl_manager_menu.addAction(resume_all_action)
        
        cancel_all_action = QAction("Cancel all", self)
        cancel_all_action.triggered.connect(self.cancel_all_downloads)
        dl_manager_menu.addAction(cancel_all_action)
        
        dl_manager_menu.addSeparator()
        
        clear_finished_action = QAction("Clear finished", self)
        clear_finished_action.triggered.connect(self.clear_finished_downloads)
        dl_manager_menu.addAction(clear_finished_action)
        
        dl_manager_menu.addSeparator()
        
        settings_action = QAction("Download settings...", self)
        settings_action.triggered.connect(self.show_download_settings)
        dl_manager_menu.addAction(settings_action)
        
        # === Player ===
        player_menu = menubar.addMenu("Player")
        
        open_player_action = QAction("Open player", self)
        open_player_action.triggered.connect(self.open_player)
        player_menu.addAction(open_player_action)
        
        player_menu.addSeparator()
        
        play_action = QAction("Play", self)
        play_action.triggered.connect(self.player_play)
        player_menu.addAction(play_action)
        
        pause_action = QAction("Pause", self)
        pause_action.triggered.connect(self.player_pause)
        player_menu.addAction(pause_action)
        
        stop_action = QAction("Stop", self)
        stop_action.triggered.connect(self.player_stop)
        player_menu.addAction(stop_action)
        
        player_menu.addSeparator()
        
        prev_track_action = QAction("Previous track", self)
        prev_track_action.triggered.connect(self.player_prev)
        player_menu.addAction(prev_track_action)
        
        next_track_action = QAction("Next track", self)
        next_track_action.triggered.connect(self.player_next)
        player_menu.addAction(next_track_action)
        
        player_menu.addSeparator()
        
        open_file_action = QAction("Open file...", self)
        open_file_action.triggered.connect(self.player_open_file)
        player_menu.addAction(open_file_action)
        
        # === View ===
        view_menu = menubar.addMenu("View")
        
        toggle_downloads_action = QAction("Show/hide downloads", self)
        toggle_downloads_action.triggered.connect(self.toggle_downloads_panel)
        view_menu.addAction(toggle_downloads_action)
        
        refresh_action = QAction("Refresh results", self)
        refresh_action.triggered.connect(self.refresh_results)
        view_menu.addAction(refresh_action)
        
        # === Help ===
        help_menu = menubar.addMenu("Help")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        # Top panel - search
        top_frame = QFrame()
        top_layout = QHBoxLayout(top_frame)
        
        search_label = QLabel("Search:")
        top_layout.addWidget(search_label)
        
        self.search_entry = QLineEdit()
        self.search_entry.setFixedWidth(300)
        self.search_entry.returnPressed.connect(self.start_search)
        top_layout.addWidget(self.search_entry)
        
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.start_search)
        top_layout.addWidget(self.search_btn)
        
        self.more_btn = QPushButton("More")
        self.more_btn.clicked.connect(self.load_more)
        self.more_btn.setEnabled(False)
        top_layout.addWidget(self.more_btn)
        
        self.status_label = QLabel("")
        top_layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.progress_bar.setFixedWidth(150)
        top_layout.addWidget(self.progress_bar)
        
        top_layout.addStretch()
        main_layout.addWidget(top_frame)
        
        # Results list
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["#", "Title", "Artist", "Downloads"])
        self.tree.setColumnWidth(0, 50)
        self.tree.setColumnWidth(1, 400)
        self.tree.setColumnWidth(2, 250)
        self.tree.setColumnWidth(3, 80)
        self.tree.itemDoubleClicked.connect(self.open_archive)
        self.tree.itemClicked.connect(self.on_tree_item_clicked)
        self.tree.setFocusPolicy(Qt.StrongFocus)
        main_layout.addWidget(self.tree)
        
        # Placeholder
        self.placeholder = QLabel("This is empty, you can fix this by searching for something")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color: gray; font-size: 12pt;")
        main_layout.addWidget(self.placeholder)
        
        # Bottom panel - downloads
        downloads_frame = QFrame()
        downloads_frame.setFrameShape(QFrame.StyledPanel)
        downloads_layout = QVBoxLayout(downloads_frame)
        
        downloads_title = QLabel("Downloads")
        downloads_title.setStyleSheet("font-weight: bold;")
        downloads_layout.addWidget(downloads_title)
        
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
        self.download_count += 1
        download_id = self.download_count
        
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame_layout = QHBoxLayout(frame)
        frame_layout.setContentsMargins(5, 2, 5, 2)
        
        name_label = QLabel(filename[:50])
        name_label.setFixedWidth(300)
        name_label.setStyleSheet("padding: 2px;")
        frame_layout.addWidget(name_label)
        
        progress = QProgressBar()
        progress.setMaximum(100)
        progress.setFixedHeight(20)
        frame_layout.addWidget(progress)
        
        status_label = QLabel("0%")
        status_label.setFixedWidth(80)
        frame_layout.addWidget(status_label)
        
        cancel_btn = QPushButton("X")
        cancel_btn.setFixedWidth(30)
        cancel_btn.clicked.connect(lambda: self.cancel_download(download_id))
        frame_layout.addWidget(cancel_btn)
        
        self.active_downloads[download_id] = {
            'filename': filename,
            'progress': progress,
            'status': status_label,
            'frame': frame,
            'cancel_btn': cancel_btn,
            'cancelled': False
        }
        
        self.downloads_layout.insertWidget(self.downloads_layout.count() - 1, frame)
        
        return download_id
    
    def update_download_progress(self, download_id, progress_value, status_text):
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            if not dl['cancelled']:
                dl['progress'].setValue(progress_value)
                dl['status'].setText(status_text)
    
    def finish_download(self, download_id, status_text):
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['progress'].setValue(100)
            dl['status'].setText(status_text)
            if status_text == "Done":
                dl['status'].setStyleSheet("color: green;")
            if 'cancel_btn' in dl:
                dl['cancel_btn'].hide()
        
        self.downloads_in_progress -= 1
        self.process_download_queue()
    
    def process_download_queue(self):
        while self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS and self.download_queue:
            queue_item = self.download_queue.pop(0)
            
            if queue_item.get('type') == 'torrent':
                self.downloads_in_progress += 1
                self._start_torrent_download(
                    queue_item['identifier'],
                    queue_item['torrent_file'],
                    queue_item['selected_indices'],
                    queue_item['download_id']
                )
            else:
                self.downloads_in_progress += 1
                self.start_download(
                    queue_item['identifier'],
                    queue_item['filename'],
                    queue_item['download_id']
                )
    
    def queue_download(self, identifier, filename, download_id):
        if self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS:
            self.downloads_in_progress += 1
            self.start_download(identifier, filename, download_id)
        else:
            self.download_queue.append({
                'identifier': identifier,
                'filename': filename,
                'download_id': download_id
            })
            if download_id in self.active_downloads:
                self.active_downloads[download_id]['status'].setText("Queued")
    
    def cancel_download(self, download_id):
        if download_id in self.active_downloads:
            dl = self.active_downloads[download_id]
            dl['cancelled'] = True
            dl['status'].setText("Cancelled")
            dl['status'].setStyleSheet("color: red;")
            if 'cancel_btn' in dl:
                dl['cancel_btn'].hide()
    
    def clear_finished_downloads(self):
        to_remove = []
        for dl_id, dl in self.active_downloads.items():
            if dl['status'].text() in ["Done", "Error", "Cancelled"]:
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
        
        self.tree.clear()
        
        # Start async search
        self._start_async_search()
    
    def _start_async_search(self):
        """Start asynchronous search to prevent UI freezing."""
        self.current_page += 1
        self.search_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self.status_label.setText("Loading...")
        
        # Create and start search thread
        self.search_thread = SearchThread(self.query, self.current_page)
        self.search_thread.search_finished_signal.connect(self._on_search_finished)
        self.search_thread.search_error_signal.connect(self._on_search_error)
        self.search_thread.start()
    
    def _on_search_finished(self, results, total):
        """Handle async search completion."""
        self.status_label.setText(f"Loaded: {len(results)} of {total}")
        self._display_results(results, total)
    
    def _on_search_error(self, error_msg):
        """Handle async search error."""
        self.status_label.setText(f"Error: {error_msg}")
        self.search_btn.setEnabled(True)
        self.current_page -= 1  # Reset page counter on error
    
    def load_more(self):
        if len(self.all_results) >= self.total and self.current_page > 0:
            return
        
        # Start async load more
        self._start_async_search()
    
    def _display_results(self, results, total):
        print(f"[DEBUG] _display_results called: results={len(results)}, total={total}")
        
        if self.current_page == 1 and len(results) > 0:
            self.placeholder.hide()
        
        if self.current_page == 1:
            self.total = total
            self.status_label.setText(f"Found: {total}")
            if total == 0:
                self.status_label.setText("Nothing found. Try a different query.")
                self.placeholder.show()
        else:
            self.status_label.setText(f"Loaded: {len(self.all_results)} / {total}")
        
        if len(results) == 0:
            self.status_label.setText("Nothing found")
            self.placeholder.show()
        
        start_idx = len(self.all_results)
        self.all_results.extend(results)
        
        for i, item in enumerate(results):
            idx = start_idx + i + 1
            title = item.get('title', '-')[:50]
            creator = item.get('creator', '???')
            # Handle creator being a list (multiple creators)
            if isinstance(creator, list):
                creator = ', '.join(str(c) for c in creator[:3])  # Join up to 3 creators
            creator = str(creator)[:30]
            downloads = item.get('downloads', 0)
            
            tree_item = QTreeWidgetItem([str(idx), title, creator, str(downloads)])
            self.tree.addTopLevelItem(tree_item)
        
        if self.total > 0:
            progress = (len(self.all_results) / self.total) * 100
            self.progress_bar.setValue(int(progress))
        
        self.search_btn.setEnabled(True)
        
        if len(self.all_results) < self.total:
            self.more_btn.setEnabled(True)
        else:
            self.progress_bar.setValue(100)
        
        # Update keyboard hints
        self.update_keyboard_hints()
    
    def update_keyboard_hints(self):
        """Update status bar with keyboard shortcuts hints."""
        if self.all_results:
            self.status_label.setText(f"Found: {self.total} | Keys: ↑↓ Navigate | Enter Open | Space More | Esc Search")
        else:
            self.status_label.setText("Enter a search query above")
    
    def open_archive(self, item, column):
        current_item = self.tree.currentItem()
        if not current_item:
            return
        
        idx = int(current_item.text(0)) - 1
        if 0 <= idx < len(self.all_results):
            item_data = self.all_results[idx]
            self.show_archive_files(item_data['identifier'], item_data.get('title', 'No title'))
    
    def show_archive_files(self, identifier, title):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Files: {title}")
        dialog.setGeometry(150, 150, 700, 500)
        
        layout = QVBoxLayout(dialog)
        
        all_files = get_all_files(identifier)
        if not all_files:
            QMessageBox.information(dialog, "Info", "No files found")
            dialog.close()
            return
        
        audio_files = get_audio_files(all_files)
        torrent_files = get_torrent_files(all_files)
        
        notebook = QTabWidget()
        layout.addWidget(notebook)
        
        # Audio tab
        if audio_files:
            audio_frame = QWidget()
            audio_layout = QVBoxLayout(audio_frame)
            
            audio_tree = QTableWidget()
            audio_tree.setColumnCount(4)
            audio_tree.setHorizontalHeaderLabels(["#", "File", "Size", "-"])
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
                selected = "-" if not already else ""
                
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
                    if item and item.text() != "-":
                        all_selected = False
                        break
                
                for i in audio_checkboxes:
                    f = audio_files[i]
                    if is_already_downloaded(identifier, f['name']):
                        continue
                    item = audio_tree.item(i, 3)
                    if item:
                        item.setText("" if all_selected else "-")
            
            def download_audio_selected():
                selected = []
                for i in audio_checkboxes:
                    item = audio_tree.item(i, 3)
                    if item and item.text() == "-":
                        selected.append(i)
                
                if not selected:
                    QMessageBox.information(dialog, "Info", "Select files to download")
                    return
                
                for idx in selected:
                    f = audio_files[idx]
                    download_id = self.add_download(f['name'])
                    self.queue_download(identifier, f['name'], download_id)
                
                for i in audio_checkboxes:
                    item = audio_tree.item(i, 3)
                    if item:
                        item.setText("")
            
            select_all_btn = QPushButton("Select all")
            select_all_btn.clicked.connect(toggle_audio_select)
            audio_btn_layout.addWidget(select_all_btn)
            
            download_btn = QPushButton("Download selected")
            download_btn.clicked.connect(download_audio_selected)
            audio_btn_layout.addWidget(download_btn)
            
            audio_layout.addWidget(audio_btn_frame)
            notebook.addTab(audio_frame, f"Audio ({len(audio_files)})")
        
        # Torrents tab
        if torrent_files:
            torrent_frame = QWidget()
            torrent_layout = QVBoxLayout(torrent_frame)
            
            torrent_tree = QTableWidget()
            torrent_tree.setColumnCount(3)
            torrent_tree.setHorizontalHeaderLabels(["#", "File", "Size"])
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
            
            torrent_btn = QPushButton("Open")
            torrent_btn.clicked.connect(open_torrent)
            torrent_layout.addWidget(torrent_btn)
            
            notebook.addTab(torrent_frame, "Torrents")
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)
        
        dialog.exec_()
    
    def start_download(self, identifier, filename, download_id):
        thread = DownloadThread(identifier, filename, download_id, self)
        thread.progress_signal.connect(self.update_download_progress, Qt.QueuedConnection)
        thread.finished_signal.connect(self.finish_download, Qt.QueuedConnection)
        
        self.download_threads[download_id] = thread
        
        thread.start()
    
    def show_torrent_files(self, identifier, torrent_file, parent_window):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Files in torrent: {torrent_file['name']}")
        dialog.setGeometry(200, 200, 600, 400)
        
        layout = QVBoxLayout(dialog)
        
        status_label = QLabel("Analyzing torrent...")
        layout.addWidget(status_label)
        dialog.show()
        
        files, error = analyze_torrent_from_archive(identifier, torrent_file)
        
        status_label.hide()
        
        if error:
            QMessageBox.critical(dialog, "Error", error)
            dialog.close()
            return
        
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["#", "File", "Size", "-"])
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
            table.setItem(i, 2, QTableWidgetItem(f"{size_mb:.1f} MB"))
            table.setItem(i, 3, QTableWidgetItem(""))
            
            checkboxes[i] = i
        
        layout.addWidget(table)
        
        btn_frame = QWidget()
        btn_layout = QHBoxLayout(btn_frame)
        
        def toggle_select():
            all_selected = True
            for i in checkboxes:
                item = table.item(i, 3)
                if item and item.text() != "-":
                    all_selected = False
                    break
            
            for i in checkboxes:
                item = table.item(i, 3)
                if item:
                    item.setText("" if all_selected else "-")
        
        def download_selected():
            selected = []
            for i in checkboxes:
                item = table.item(i, 3)
                if item and item.text() == "-":
                    selected.append(i)
            
            if not selected:
                QMessageBox.information(dialog, "Info", "Select files to download")
                return
            
            download_id = self.add_download(f"Torrent: {torrent_file['name']}")
            self.queue_torrent_download(identifier, torrent_file, selected, download_id)
            dialog.close()
        
        select_btn = QPushButton("Select all")
        select_btn.clicked.connect(toggle_select)
        btn_layout.addWidget(select_btn)
        
        download_btn = QPushButton("Download selected")
        download_btn.clicked.connect(download_selected)
        btn_layout.addWidget(download_btn)
        
        layout.addWidget(btn_frame)
        
        dialog.exec_()
    
    def queue_torrent_download(self, identifier, torrent_file, selected_indices, download_id):
        if self.downloads_in_progress < MAX_CONCURRENT_DOWNLOADS:
            self.downloads_in_progress += 1
            self._start_torrent_download(identifier, torrent_file, selected_indices, download_id)
        else:
            self.download_queue.append({
                'type': 'torrent',
                'identifier': identifier,
                'torrent_file': torrent_file,
                'selected_indices': selected_indices,
                'download_id': download_id
            })
            if download_id in self.active_downloads:
                self.active_downloads[download_id]['status'].setText("Queued")
    
    def _start_torrent_download(self, identifier, torrent_file, selected_indices, download_id):
        def progress_callback(msg):
            pass
        
        def finish_callback():
            self.finish_download(download_id, "Done")
        
        download_selected_from_torrent(identifier, torrent_file, selected_indices, progress_callback, finish_callback)
    
    # === Menu handlers ===
    
    def open_download_folder(self):
        import subprocess
        try:
            if os.name == 'nt':
                os.startfile(DOWNLOAD_FOLDER)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', DOWNLOAD_FOLDER])
            else:
                subprocess.Popen(['xdg-open', DOWNLOAD_FOLDER])
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to open folder: {e}")
    
    def toggle_downloads_panel(self):
        if hasattr(self, 'downloads_scroll'):
            self.downloads_scroll.setVisible(not self.downloads_scroll.isVisible())
    
    def refresh_results(self):
        if self.query and self.all_results:
            self.start_search()
        else:
            QMessageBox.information(self, "Info", "No results to refresh")
    
    def pause_all_downloads(self):
        if not self.active_downloads:
            QMessageBox.information(self, "Info", "No active downloads")
            return
        
        paused = 0
        for dl_id, dl in self.active_downloads.items():
            status = dl['status'].text()
            if status not in ["Done", "Error", "Cancelled", "Queued"]:
                if dl_id in self.download_threads:
                    thread = self.download_threads[dl_id]
                    thread.stop()
                dl['status'].setText("Paused")
                dl['status'].setStyleSheet("color: orange;")
                paused += 1
        
        if paused > 0:
            QMessageBox.information(self, "Info", f"Paused downloads: {paused}")
        else:
            QMessageBox.information(self, "Info", "No active downloads to pause")
    
    def resume_all_downloads(self):
        QMessageBox.information(self, "Info", "Resume function temporarily unavailable")
    
    def cancel_all_downloads(self):
        if not self.active_downloads:
            QMessageBox.information(self, "Info", "No active downloads")
            return
        
        reply = QMessageBox.question(
            self, "Confirm",
            "Are you sure you want to cancel all downloads?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            cancelled = 0
            for dl_id in list(self.active_downloads.keys()):
                self.cancel_download(dl_id)
                cancelled += 1
            QMessageBox.information(self, "Info", f"Cancelled downloads: {cancelled}")
    
    def show_download_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Download Settings")
        dialog.setFixedSize(400, 250)
        
        layout = QVBoxLayout(dialog)
        
        folder_layout = QHBoxLayout()
        folder_label = QLabel("Download folder:")
        folder_layout.addWidget(folder_label)
        
        folder_value = QLabel(DOWNLOAD_FOLDER)
        folder_value.setStyleSheet("color: #888;")
        folder_layout.addWidget(folder_value)
        
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self.open_download_folder)
        folder_layout.addWidget(open_btn)
        
        layout.addLayout(folder_layout)
        
        layout.addWidget(QLabel(""))
        
        max_layout = QHBoxLayout()
        max_label = QLabel("Max concurrent downloads:")
        max_layout.addWidget(max_label)
        
        max_value = QLabel(str(MAX_CONCURRENT_DOWNLOADS))
        max_value.setStyleSheet("color: #0078d4; font-weight: bold;")
        max_layout.addWidget(max_value)
        
        max_layout.addStretch()
        layout.addLayout(max_layout)
        
        layout.addWidget(QLabel(""))
        
        torrent_layout = QHBoxLayout()
        torrent_label = QLabel("Torrent support:")
        torrent_layout.addWidget(torrent_label)
        
        torrent_status = QLabel("Enabled" if TORRENT_ENABLED else "Disabled")
        torrent_status.setStyleSheet("color: green;" if TORRENT_ENABLED else "color: red;")
        torrent_layout.addWidget(torrent_status)
        
        torrent_layout.addStretch()
        layout.addLayout(torrent_layout)
        
        tray_layout = QHBoxLayout()
        tray_label = QLabel("System tray:")
        tray_layout.addWidget(tray_label)
        
        tray_status = QLabel("Enabled" if TRAY_ENABLED else "Disabled")
        tray_status.setStyleSheet("color: green;" if TRAY_ENABLED else "color: red;")
        tray_layout.addWidget(tray_status)
        
        tray_layout.addStretch()
        layout.addLayout(tray_layout)
        
        layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)
        
        dialog.exec_()
    
    def show_about(self):
        QMessageBox.about(
            self,
            "About LakArchive",
            "<h3>LakArchive</h3>"
            "<p>Version 1.0</p>"
            "<p>Application for searching and downloading music from Archive.org</p>"
            "<hr>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Search music by title and artist</li>"
            "<li>Download audio files (MP3, FLAC, OGG, WAV)</li>"
            "<li>Torrent download support</li>"
            "<li>System tray</li>"
            "<li>Dark theme</li>"
            "</ul>"
        )
    
    # === Download Manager ===
    
    def show_download_manager(self):
        self.downloads_scroll.setVisible(True)
        QMessageBox.information(
            self,
            "Download Manager",
            "Download manager is shown on the bottom panel.\n\n"
            "Here you can:\n"
            "- See progress of all downloads\n"
            "- Cancel individual downloads\n"
            "- Pause and resume downloads"
        )
    
    # === Player ===
    
    def open_player(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Audio Player")
        dialog.setFixedSize(500, 300)
        
        layout = QVBoxLayout(dialog)
        
        self.player_track_label = QLabel("Track: not selected")
        self.player_track_label.setAlignment(Qt.AlignCenter)
        self.player_track_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        layout.addWidget(self.player_track_label)
        
        time_layout = QHBoxLayout()
        self.player_current_time = QLabel("00:00")
        time_layout.addWidget(self.player_current_time)
        
        self.player_slider = QSlider(Qt.Horizontal)
        self.player_slider.setRange(0, 100)
        time_layout.addWidget(self.player_slider)
        
        self.player_total_time = QLabel("00:00")
        time_layout.addWidget(self.player_total_time)
        
        layout.addLayout(time_layout)
        
        controls_layout = QHBoxLayout()
        
        prev_btn = QPushButton("<<")
        prev_btn.clicked.connect(self.player_prev)
        controls_layout.addWidget(prev_btn)
        
        play_btn = QPushButton("Play")
        play_btn.clicked.connect(self.player_play)
        controls_layout.addWidget(play_btn)
        
        pause_btn = QPushButton("Pause")
        pause_btn.clicked.connect(self.player_pause)
        controls_layout.addWidget(pause_btn)
        
        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(self.player_stop)
        controls_layout.addWidget(stop_btn)
        
        next_btn = QPushButton(">>")
        next_btn.clicked.connect(self.player_next)
        controls_layout.addWidget(next_btn)
        
        layout.addLayout(controls_layout)
        
        volume_layout = QHBoxLayout()
        volume_label = QLabel("Volume:")
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
        
        btn_layout = QHBoxLayout()
        
        open_file_btn = QPushButton("Open file...")
        open_file_btn.clicked.connect(self.player_open_file)
        btn_layout.addWidget(open_file_btn)
        
        open_folder_btn = QPushButton("Open folder")
        open_folder_btn.clicked.connect(self.open_download_folder)
        btn_layout.addWidget(open_folder_btn)
        
        btn_layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
        
        dialog.exec_()
    
    def player_play(self):
        QMessageBox.information(self, "Player", "Playback: feature under development.\n\nUse external player for playback.")
    
    def player_pause(self):
        QMessageBox.information(self, "Player", "Pause: feature under development.")
    
    def player_stop(self):
        QMessageBox.information(self, "Player", "Stop: feature under development.")
    
    def player_prev(self):
        QMessageBox.information(self, "Player", "Previous track: feature under development.")
    
    def player_next(self):
        QMessageBox.information(self, "Player", "Next track: feature under development.")
    
    def player_open_file(self):
        from PyQt5.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            DOWNLOAD_FOLDER,
            "Audio files (*.mp3 *.flac *.ogg *.wav);;All files (*.*)"
        )
        if file_path:
            self.player_track_label.setText(f"Track: {os.path.basename(file_path)}")
            QMessageBox.information(
                self,
                "Player",
                f"Selected file: {os.path.basename(file_path)}\n\nPlayback feature under development."
            )


# === Dark theme ===
DARK_STYLESHEET = """
QMainWindow {
    background-color: #1e1e1e;
}

QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    selection-background-color: #0078d4;
    selection-color: #ffffff;
}

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

QLabel {
    background-color: transparent;
    color: #e0e0e0;
}

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

QFrame {
    background-color: #1e1e1e;
}

QFrame[frameShape="4"], QFrame[frameShape="5"] {
    background-color: #252526;
    border: 1px solid #3d3d3d;
    border-radius: 4px;
}

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

QDialog {
    background-color: #1e1e1e;
}

QSystemTrayIcon {
    background-color: #1e1e1e;
}
"""


def main():
    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    # Check server connection before starting
    if not check_server_connection():
        QMessageBox.critical(
            None,
            "Connection Error",
            "Не удалось подключиться к серверу archive.org.\n\n"
            "Проверьте подключение к интернету и попробуйте снова."
        )
        sys.exit(1)
    
    app.setStyleSheet(DARK_STYLESHEET)
    
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
    
    # Check if first run and show setup wizard if needed
    if is_first_run():
        print("[DEBUG] First run detected, showing setup wizard")
        # Import QFileDialog for the wizard
        from PyQt5.QtWidgets import QFileDialog
        
        dialog = QDialog()
        dialog.setWindowTitle("LakArchive - Setup Wizard")
        dialog.setFixedSize(500, 300)
        dialog.setModal(True)
        
        layout = QVBoxLayout(dialog)
        
        # Title
        title = QLabel("<h2>Welcome to LakArchive!</h2>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # Description
        desc = QLabel("Let's set up your download folder.")
        desc.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc)
        
        layout.addWidget(QLabel(""))
        
        # Folder selection
        folder_layout = QHBoxLayout()
        folder_label = QLabel("Download folder:")
        folder_layout.addWidget(folder_label)
        
        folder_path = QLineEdit()
        folder_path.setText(DOWNLOAD_FOLDER)
        folder_path.setReadOnly(True)
        folder_layout.addWidget(folder_path)
        
        browse_btn = QPushButton("Browse...")
        def browse_folder():
            folder = QFileDialog.getExistingDirectory(
                dialog, "Select Download Folder", 
                DOWNLOAD_FOLDER if os.path.exists(DOWNLOAD_FOLDER) else os.path.expanduser("~")
            )
            if folder:
                folder_path.setText(folder)
                folder_path.setToolTip(folder)
        
        browse_btn.clicked.connect(browse_folder)
        folder_layout.addWidget(browse_btn)
        
        layout.addLayout(folder_layout)
        
        layout.addWidget(QLabel(""))
        
        # Info
        info = QLabel("You can change these settings later in the application.")
        info.setStyleSheet("color: #888; font-size: 10pt;")
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)
        
        layout.addStretch()
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)
        
        finish_btn = QPushButton("Save & Continue")
        finish_btn.setDefault(True)
        
        def save_and_close():
            # Save settings
            new_settings = {
                "download_folder": folder_path.text(),
                "results_per_page": 20,
                "max_concurrent_downloads": 5,
                "first_run": False
            }
            
            # Create folder if it doesn't exist
            folder = folder_path.text()
            if folder:
                try:
                    os.makedirs(folder, exist_ok=True)
                except:
                    pass
            
            if save_config(new_settings):
                dialog.accept()
            else:
                QMessageBox.warning(dialog, "Error", "Failed to save configuration!")
        
        finish_btn.clicked.connect(save_and_close)
        btn_layout.addWidget(finish_btn)
        
        layout.addLayout(btn_layout)
        
        # Show dialog
        result = dialog.exec_()
        
        if result != QDialog.Accepted:
            # User cancelled - exit application
            print("[DEBUG] Setup wizard cancelled, exiting")
            sys.exit(0)
        
        print("[DEBUG] Setup completed, continuing to main window")
    
    window = ArchiveMusicApp()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
