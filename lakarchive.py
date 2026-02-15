import os
import sys
import requests
import threading
import time
import sqlite3
from datetime import datetime
from urllib.parse import quote
import termios
import tty

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
MAX_LINES = 12

# === База данных (без изменений) ===
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

# === Вспомогательные функции (без изменений) ===
def read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch += sys.stdin.read(2)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def clear_screen():
    os.system('clear' if os.name == 'posix' else 'cls')

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
    """Получает ВСЕ файлы из архива (включая .torrent)"""
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
        # Скачиваем .torrent во временный файл
        temp_path = os.path.join(DOWNLOAD_FOLDER, f".temp_{torrent_file['name']}")
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(temp_path, 'wb') as f:
            f.write(r.content)

        # Парсим метаданные
        info = lt.torrent_info(temp_path)
        files = []
        for i, f in enumerate(info.files()):
            files.append({
                'index': i,
                'path': f.path,
                'size': f.size
            })
        
        # Удаляем временный файл
        os.remove(temp_path)
        return files, None
    except Exception as e:
        return None, str(e)

# === Загрузка выбранных файлов из торрента ===
def download_selected_from_torrent(identifier, torrent_file, selected_indices):
    """Скачивает .torrent, затем выбранные файлы через libtorrent"""
    def _download():
        if not TORRENT_ENABLED:
            print("❌ libtorrent не установлен")
            return

        # Скачиваем .torrent
        url = f"https://archive.org/download/{identifier}/{torrent_file['name']}"
        torrent_path = os.path.join(DOWNLOAD_FOLDER, torrent_file['name'])
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(torrent_path, 'wb') as f:
                f.write(r.content)
        except Exception as e:
            print(f"❌ Не удалось скачать .torrent: {e}")
            return

        try:
            ses = lt.session()
            ses.listen_on(6881, 6891)
            info = lt.torrent_info(torrent_path)
            handle = ses.add_torrent({'ti': info, 'save_path': DOWNLOAD_FOLDER})

            # Отключаем всё, кроме выбранных
            priorities = [0] * info.num_files()
            for idx in selected_indices:
                if 0 <= idx < len(priorities):
                    priorities[idx] = 4
            handle.prioritize_files(priorities)
            handle.resume()

            print(f"\n📥 Загрузка {len(selected_indices)} файлов...")
            while not handle.is_seed():
                s = handle.status()
                print(f"\rПрогресс: {s.progress * 100:.1f}% | Скорость: {s.download_rate / 1000:.1f} kB/s", end='', flush=True)
                time.sleep(1)
                if s.progress >= 1.0:
                    break
            print("\n✅ Готово!")
        except Exception as e:
            print(f"\n❌ Ошибка: {e}")

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()

# === Выбор из архива: аудио ИЛИ торрент ===
def choose_from_archive(identifier, title):
    clear_screen()
    print(f"Архив: {title}")
    print("Загрузка списка файлов...")

    all_files = get_all_files(identifier)
    if not all_files:
        input("Файлы не найдены. Нажмите Enter...")
        return

    audio_files = get_audio_files(all_files)
    torrent_files = get_torrent_files(all_files)

    while True:
        clear_screen()
        print(f"Архив: {title}")
        options = []

        if audio_files:
            print("Аудиофайлы:")
            for i, f in enumerate(audio_files):
                size = human_size(int(f.get('size', 0)))
                already = is_already_downloaded(identifier, f['name'])
                mark = "[✓]" if already else "[ ]"
                print(f"  A{i+1}. {mark} {f['name']} | {size}")
            options.append('audio')
            print()

        if torrent_files:
            print("Торренты:")
            for i, f in enumerate(torrent_files):
                size = human_size(int(f.get('size', 0)))
                print(f"  T{i+1}. {f['name']} | {size}")
            options.append('torrent')
            print()

        if not options:
            print("Нет аудиофайлов или торрентов.")
            input("Нажмите Enter...")
            return

        print("Введите команду:")
        if 'audio' in options:
            print("  a<N> — скачать аудиофайл (например: a1)")
        if 'torrent' in options:
            print("  t<N> — открыть торрент (например: t1)")
        print("  q — выход")

        choice = input("> ").strip().lower()

        if choice == 'q':
            return

        # Аудио
        if choice.startswith('a') and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < len(audio_files):
                f = audio_files[idx]
                if is_already_downloaded(identifier, f['name']):
                    print("Файл уже скачан.")
                else:
                    print("Загрузка запущена...")
                    download_file_simple(identifier, f['name'])
                input("Нажмите Enter...")
                return

        # Торрент
        if choice.startswith('t') and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < len(torrent_files):
                torrent_file = torrent_files[idx]
                print(f"Анализ торрента: {torrent_file['name']}...")
                files, error = analyze_torrent_from_archive(identifier, torrent_file)
                if error:
                    print(f"❌ {error}")
                    input("Нажмите Enter...")
                    continue

                # Выбор файлов из торрента
                while True:
                    clear_screen()
                    print(f"Файлы в торренте: {torrent_file['name']}")
                    print("-" * 70)
                    for i, f in enumerate(files):
                        size_mb = f['size'] / (1024**2)
                        print(f"{i+1:2}. {f['path']} ({size_mb:.1f} МБ)")
                    print("-" * 70)
                    print("Введите номера через запятую (1,3) или 'q' для отмены:")

                    sel = input("> ").strip()
                    if sel.lower() == 'q':
                        break
                    try:
                        indices = [int(x.strip()) - 1 for x in sel.split(',')]
                        if all(0 <= i < len(files) for i in indices):
                            download_selected_from_torrent(identifier, torrent_file, indices)
                            input("Загрузка запущена. Нажмите Enter...")
                            return
                        else:
                            print("Неверные номера.")
                    except ValueError:
                        print("Неверный формат.")
                    input("Нажмите Enter...")
                return

        print("Неверная команда.")
        input("Нажмите Enter...")

# === Простая загрузка аудио (без tqdm) ===
def download_file_simple(identifier, filename):
    def _download():
        url = f"https://archive.org/download/{identifier}/{filename}"
        clean_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)
        path = os.path.join(DOWNLOAD_FOLDER, clean_name)
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            with open(path, 'wb') as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            add_to_db(identifier, filename, path)
            print(f"\n✅ Сохранено: {path}")
        except Exception as e:
            print(f"\n❌ Ошибка: {e}")
    threading.Thread(target=_download, daemon=True).start()

# === Основной цикл (без изменений, кроме вызова choose_from_archive) ===
def main():
    init_db()
    clear_screen()
    try:
        query = input("Введите запрос: ").strip()
    except KeyboardInterrupt:
        return
    if not query:
        return

    all_results = []
    total = 0
    page = 0

    def load_more():
        nonlocal page, total
        page += 1
        results, t = fetch_page(query, page)
        if page == 1:
            total = t
        all_results.extend(results)
        return len(results)

    if load_more() == 0:
        print("Ничего не найдено.")
        input("Нажмите Enter...")
        return

    selected = 0
    offset = 0

    while True:
        if selected >= len(all_results) - 3 and len(all_results) < total:
            load_more()

        if selected < offset:
            offset = selected
        elif selected >= offset + MAX_LINES:
            offset = selected - MAX_LINES + 1
        if offset < 0:
            offset = 0

        clear_screen()
        cols = 100
        try:
            _, cols = os.popen('stty size', 'r').read().split()
            cols = int(cols)
        except:
            pass

        print("┌" + "─" * (cols - 2) + "┐")
        title = f"Результаты: '{query}' (всего: {total})"
        print(f"│{title[:cols-2].ljust(cols-2)}│")
        print("├" + "─" * (cols - 2) + "┤")

        visible = all_results[offset:offset + MAX_LINES]
        for i in range(MAX_LINES):
            if i < len(visible):
                idx = offset + i
                item = visible[i]
                line = f"{idx+1:3}. {item.get('title', '—')} — {item.get('creator', '???')}"
                if idx == selected:
                    line = "▶ " + line[2:]
                else:
                    line = "  " + line[2:]
                print(f"│{line[:cols-2].ljust(cols-2)}│")
            else:
                print(f"│{''.ljust(cols-2)}│")

        print("├" + "─" * (cols - 2) + "┤")
        status = "↑↓ — навигация | Enter — открыть | q — выход"
        print(f"│{status[:cols-2].ljust(cols-2)}│")
        print("└" + "─" * (cols - 2) + "┘")

        key = read_key()
        if key == 'q':
            break
        elif key == '\x1b[B' and selected < len(all_results) - 1:
            selected += 1
        elif key == '\x1b[A' and selected > 0:
            selected -= 1
        elif key in ('\r', '\n'):
            item = all_results[selected]
            choose_from_archive(item['identifier'], item.get('title', 'Без названия'))

    clear_screen()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nВыход.")
