import os
import sys
import requests
from urllib.parse import quote
from tqdm import tqdm
import termios
import tty

# === Настройки ===
DOWNLOAD_FOLDER = os.path.expanduser("~/Music/free_archive")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
RESULTS_PER_PAGE = 20
MAX_LINES = 12

# === Вспомогательные функции ===
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
        'q': f'(collection:(etree OR audio_music OR opensource_audio)) AND (title:({quote(query)}) OR creator:({quote(query)}))',
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

def get_audio_files(identifier):
    try:
        data = requests.get(f"https://archive.org/metadata/{identifier}", timeout=10).json()
        files = data.get('files', [])
        audio_files = [
            f for f in files
            if f.get('format') in ['VBR MP3', 'MP3', 'FLAC', 'Ogg Vorbis', 'WAVE']
            and f.get('source') == 'original'
            and f.get('name')
        ]
        # Сортировка: MP3 перед FLAC, маленькие перед большими
        def sort_key(f):
            fmt = f.get('format', '')
            size = int(f.get('size', 0))
            is_mp3 = 'MP3' in fmt
            return (0 if is_mp3 else 1, size)
        audio_files.sort(key=sort_key)
        return audio_files
    except:
        return []

def download_file(identifier, filename):
    url = f"https://archive.org/download/{identifier}/{filename}"
    path = os.path.join(DOWNLOAD_FOLDER, filename)
    print(f"\n Скачивание: {filename}")
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        with open(path, 'wb') as f, tqdm(total=total, unit='B', unit_scale=True, desc="Загрузка") as pb:
            for chunk in r.iter_content(8192):
                f.write(chunk)
                pb.update(len(chunk))
        print(f" Сохранено: {path}")
    except Exception as e:
        print(f" Ошибка: {e}")

# === Выбор файла (или all) ===
def choose_file_from_archive(identifier, title):
    clear_screen()
    print(f" Архив: {title}")
    print("Загрузка списка файлов...")
    
    files = get_audio_files(identifier)
    if not files:
        input("❌ Аудиофайлы не найдены. Нажмите Enter...")
        return None, False

    while True:
        clear_screen()
        print(f" Архив: {title}")
        print("Найдены аудиофайлы:")
        print("-" * 70)
        for i, f in enumerate(files):
            name = f['name']
            fmt = f.get('format', '???')
            size = human_size(int(f.get('size', 0)))
            print(f"{i+1:2}. {name} | {fmt} | {size}")
        print("-" * 70)
        print("Введите номер файла, 'all' для скачивания всех или 'q' для отмены:")

        choice = input("> ").strip().lower()
        
        if choice == 'q':
            return None, False
        
        elif choice == 'all':
            return None, True
        
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(files):
                return files[idx]['name'], False
            else:
                print("Неверный номер.")
        else:
            print("Неизвестная команда. Используйте номер, 'all' или 'q'.")
        
        input("Нажмите Enter...")

# === Основной цикл ===
def main():
    clear_screen()
    try:
        query = input("🔍 Введите запрос (только бесплатная музыка): ").strip()
    except KeyboardInterrupt:
        print("\nВыход.")
        return

    if not query:
        print("Пустой запрос.")
        return

    # Загрузка результатов
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

    # TUI навигация
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

        # Отрисовка
        clear_screen()
        cols = 100
        try:
            _, cols = os.popen('stty size', 'r').read().split()
            cols = int(cols)
        except:
            pass

        print("┌" + "─" * (cols - 2) + "┐")
        title = f"🎵 Результаты: '{query}' (всего: {total})"
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
        status = "↑↓ — навигация | Enter — выбрать архив | q — выход"
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
            identifier = item['identifier']
            title = item.get('title', 'Без названия')
            
            filename, download_all = choose_file_from_archive(identifier, title)
            
            if download_all:
                all_files = get_audio_files(identifier)
                if all_files:
                    print(f"\n Скачивание всех {len(all_files)} файлов из '{title}'...")
                    for f in all_files:
                        download_file(identifier, f['name'])
                    input("\n Все файлы скачаны! Нажмите Enter...")
                else:
                    input("Нет файлов для скачивания. Нажмите Enter...")
            elif filename:
                download_file(identifier, filename)

    clear_screen()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear_screen()
        print("\nВыход.")
