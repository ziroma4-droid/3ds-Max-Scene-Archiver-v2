#!/usr/bin/env python3
"""
Max Scene Packager v2.7
Автор: @RomanCG
GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2
Telegram: https://t.me/Romak04
Email: romancg@yandex.ru
"""

import os
import re
import sys
import ctypes
import zipfile
import logging
import webbrowser
from datetime import datetime
from collections import defaultdict
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading

try:
    import olefile
    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False


def enable_dpi_awareness():
    """Let Windows render Tk directly at the active monitor's native DPI."""
    if sys.platform != 'win32':
        return False

    try:
        # Windows 10+: Per-Monitor V2 handles mixed-DPI monitor setups.
        per_monitor_v2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return True
    except (AttributeError, OSError):
        pass

    try:
        # Windows 8.1 fallback.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, None):
            return True
    except (AttributeError, OSError):
        pass

    try:
        # Legacy fallback for older Windows versions.
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


class PathExtractor:
    
    TEXTURE_EXT = {'.jpg', '.jpeg', '.png', '.tga', '.tif', '.tiff', '.bmp', 
                   '.hdr', '.exr', '.psd', '.dds', '.gif', '.webp', '.tx', '.tex'}
    
    IES_EXT = {'.ies'}
    
    PROXY_EXT = {'.vrmesh', '.vrscene', '.vrlmap', '.vrmap', '.cgeo', '.cproxy',
                 '.abc', '.obj', '.fbx', '.rs', '.rstex'}
    
    SCENE_EXT = {'.max', '.chr', '.cat'}
    
    CACHE_EXT = {'.pc2', '.mdd', '.mc', '.xml', '.bin', '.bif', '.tyc', '.tyflow'}
    
    AUDIO_EXT = {'.wav', '.mp3', '.aif', '.aiff', '.ogg'}

    LUT_EXT = {'.cube', '.3dl', '.look', '.lut', '.csp', '.cub'}

    ALL_EXT = TEXTURE_EXT | IES_EXT | PROXY_EXT | SCENE_EXT | CACHE_EXT | AUDIO_EXT | LUT_EXT

    ASCII_ABSOLUTE_PATTERN = rb'[A-Za-z]:[\\\/][^\x00-\x1f"*<>|]{5,260}\.[A-Za-z0-9]{1,10}'
    ASCII_UNC_PATTERN = (
        rb'[\\\/]{2}[^\\\/\x00-\x1f"*<>|:]+[\\\/]'
        rb'[^\\\/\x00-\x1f"*<>|:]+[\\\/]'
        rb'[^\x00-\x1f"*<>|:]{1,260}\.[A-Za-z0-9]{1,10}'
    )
    ASCII_RELATIVE_PATTERN = (
        rb'(?:^|[\x00-\x1f])'
        rb'((?:\.{1,2}[\\\/])?'
        rb'(?:[^\\\/\x00-\x1f"*<>|:]+[\\\/])*'
        rb'[^\\\/\x00-\x1f"*<>|:]+\.[A-Za-z0-9]{1,10})'
        rb'(?=$|[\x00-\x1f])'
    )

    
    def extract_paths(self, data, exclude_ext=None, include_relative=True):
        if exclude_ext is None:
            exclude_ext = set()
        
        paths = set()
        
        # ASCII
        try:
            for pattern in (self.ASCII_ABSOLUTE_PATTERN, self.ASCII_UNC_PATTERN):
                for match in re.finditer(pattern, data):
                    try:
                        path = match.group(0).decode('ascii', errors='ignore')
                        path = self.normalize(path)
                        if self.is_valid(path, exclude_ext):
                            paths.add(path)
                    except Exception:
                        pass

            if include_relative:
                for match in re.finditer(self.ASCII_RELATIVE_PATTERN, data):
                    try:
                        path = match.group(1).decode('ascii', errors='ignore')
                        path = self.normalize(path)
                        if self.is_valid(path, exclude_ext):
                            paths.add(path)
                    except Exception:
                        pass
        except Exception:
            pass
        
        # UTF-16
        try:
            i = 0
            while i < len(data) - 10:
                if (data[i] in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz' and
                    i + 5 < len(data) and
                    data[i+1] == 0 and data[i+2] == ord(':') and 
                    data[i+3] == 0 and data[i+4] == ord('\\') and data[i+5] == 0):
                    
                    path_bytes = bytearray()
                    j = i
                    while j < min(i + 520, len(data) - 1):
                        if data[j] == 0 and data[j+1] == 0:
                            break
                        if data[j+1] == 0 and data[j] < 32:
                            break
                        path_bytes.append(data[j])
                        path_bytes.append(data[j+1])
                        j += 2
                    
                    try:
                        path = bytes(path_bytes).decode('utf-16-le', errors='ignore').strip('\x00')
                        path = self.normalize(path)
                        if self.is_valid(path, exclude_ext):
                            paths.add(path)
                    except Exception:
                        pass
                    i = j
                else:
                    i += 1
        except Exception:
            pass

        # Null-terminated UTF-16 strings, including relative and UNC paths.
        try:
            for path in self.extract_utf16_strings(data):
                path = self.normalize(path)
                if not include_relative and not self.is_absolute(path):
                    continue
                if self.is_valid(path, exclude_ext):
                    paths.add(path)
        except Exception:
            pass
        
        return paths

    def extract_utf16_strings(self, data):
        strings = set()

        # Try both byte alignments because OLE stream data does not guarantee
        # that a UTF-16 string starts at an even stream offset.
        for offset in (0, 1):
            chars = []

            def flush():
                if 5 <= len(chars) <= 520:
                    strings.add(''.join(chars))
                chars.clear()

            for i in range(offset, len(data) - 1, 2):
                codepoint = data[i] | (data[i + 1] << 8)
                if codepoint == 0:
                    flush()
                    continue

                char = chr(codepoint)
                if codepoint >= 32 and char.isprintable():
                    chars.append(char)
                    if len(chars) > 520:
                        flush()
                else:
                    flush()

            flush()

        return strings
    
    def normalize(self, path):
        if not path:
            return ""
        path = path.strip('\x00\r\n\t ')
        path = path.replace('/', '\\')
        return path
    
    def is_valid(self, path, exclude_ext):
        if not path or len(path) < 5:
            return False

        is_drive_absolute = bool(re.match(r'^[A-Za-z]:\\', path))
        is_unc = path.startswith('\\\\')
        is_relative = not is_drive_absolute and not is_unc

        if is_unc:
            # A usable UNC asset path needs server, share and file components.
            if len([part for part in path[2:].split('\\') if part]) < 3:
                return False
        elif is_relative:
            if path.startswith('\\') or ':' in path:
                return False

        if any(char in path for char in '"*<>|'):
            return False

        ext = os.path.splitext(path)[1].lower()
        if ext in exclude_ext:
            return False
        if ext not in self.ALL_EXT:
            return False
        return True

    def is_absolute(self, path):
        path = self.normalize(path)
        return bool(re.match(r'^[A-Za-z]:\\', path)) or path.startswith('\\\\')

    def resolve(self, path, base_dir):
        path = self.normalize(path)
        if re.match(r'^[A-Za-z]:\\', path) or path.startswith('\\\\'):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(base_dir, path))
    
    def categorize(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in self.TEXTURE_EXT:
            return 'texture'
        elif ext in self.IES_EXT:
            return 'ies'
        elif ext in self.PROXY_EXT:
            return 'proxy'
        elif ext in self.SCENE_EXT:
            return 'xref'
        elif ext in self.CACHE_EXT:
            return 'cache'
        elif ext in self.AUDIO_EXT:
            return 'audio'
        elif ext in self.LUT_EXT:
            return 'lut'
        return None


class MaxParser:
    
    def __init__(self, log_func=None):
        self.log_func = log_func
        self.extractor = PathExtractor()
        self.local_asset_indexes = {}
    
    def log(self, msg):
        if self.log_func:
            self.log_func(msg)
    
    def parse(self, filepath):
        result = defaultdict(set)
        visited = set()
        self.local_asset_indexes = {}
        root_scene = os.path.normcase(os.path.abspath(filepath))

        self.parse_scene_recursive(filepath, result, visited, root_scene)

        total = sum(len(v) for v in result.values())
        self.log(f"Найдено: {total}")

        return dict(result)

    def build_local_asset_index(self, scene_dir):
        scene_dir = os.path.abspath(scene_dir)
        index_key = os.path.normcase(scene_dir)
        if index_key in self.local_asset_indexes:
            return self.local_asset_indexes[index_key]

        index = defaultdict(list)
        try:
            for root, dirs, filenames in os.walk(scene_dir):
                dirs.sort(key=str.casefold)
                for filename in sorted(filenames, key=str.casefold):
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in self.extractor.ALL_EXT:
                        index[filename.lower()].append(os.path.join(root, filename))
        except OSError as e:
            self.log(f"Не удалось просканировать папку сцены: {scene_dir}: {e}")

        self.local_asset_indexes[index_key] = index
        return index

    @staticmethod
    def path_parts(path):
        path = path.replace('/', '\\')
        drive, tail = os.path.splitdrive(path)
        parts = [part.lower() for part in tail.split('\\') if part]

        # For UNC paths, ignore server/share when comparing with local files.
        if not drive and path.startswith('\\\\') and len(parts) > 2:
            parts = parts[2:]

        return parts

    @staticmethod
    def common_suffix_length(left, right):
        count = 0
        for left_part, right_part in zip(reversed(left), reversed(right)):
            if left_part != right_part:
                break
            count += 1
        return count

    def find_near_scene(self, path, scene_dir):
        filename = path.replace('/', '\\').rsplit('\\', 1)[-1]
        if not filename:
            return None

        candidates = self.build_local_asset_index(scene_dir).get(filename.lower(), [])
        if not candidates:
            return None

        requested_parts = self.path_parts(path)

        def score(candidate):
            try:
                relative = os.path.relpath(candidate, scene_dir)
            except ValueError:
                relative = candidate

            candidate_parts = self.path_parts(relative)
            return (
                -self.common_suffix_length(requested_parts, candidate_parts),
                len(candidate_parts),
                candidate.lower(),
            )

        return sorted(candidates, key=score)[0]

    def resolve_scene_asset(self, path, scene_dir):
        resolved_path = self.extractor.resolve(path, scene_dir)
        if os.path.exists(resolved_path):
            return resolved_path

        nearby_path = self.find_near_scene(path, scene_dir)
        if nearby_path:
            if os.path.normcase(os.path.abspath(nearby_path)) != os.path.normcase(
                os.path.abspath(resolved_path)
            ):
                self.log(
                    "Найдено рядом со сценой: "
                    f"{os.path.basename(path)} -> {nearby_path}"
                )
            return nearby_path

        return resolved_path

    def parse_scene_recursive(self, filepath, result, visited, root_scene):
        filepath = os.path.abspath(filepath)
        scene_key = os.path.normcase(filepath)

        if scene_key in visited:
            return
        visited.add(scene_key)
        
        if not os.path.exists(filepath):
            self.log(f"Файл не найден: {filepath}")
            return
        
        size_mb = os.path.getsize(filepath) / 1024 / 1024
        self.log(f"Анализ: {os.path.basename(filepath)} ({size_mb:.1f} MB)")
        
        # XRef scene extensions are deliberately included and processed below.
        exclude = set()
        scene_dir = os.path.dirname(os.path.abspath(filepath))
        xrefs = set()

        def add_paths(data, include_relative=True):
            paths = self.extractor.extract_paths(
                data, exclude, include_relative=include_relative
            )
            for path in paths:
                resolved_path = self.resolve_scene_asset(path, scene_dir)
                resolved_key = os.path.normcase(os.path.abspath(resolved_path))
                category = self.extractor.categorize(resolved_path)

                # Do not add the main scene if a circular XRef points back to it.
                if category == 'xref' and resolved_key == root_scene:
                    continue

                if category:
                    result[category].add(resolved_path)
                    if category == 'xref':
                        xrefs.add(resolved_path)
        
        try:
            if HAS_OLEFILE:
                self.log("OLE парсер...")
                ole = olefile.OleFileIO(filepath)
                streams = ole.listdir()
                metadata_streams = [
                    stream for stream in streams
                    if stream and stream[-1].lower().startswith('fileassetmetadata')
                ]

                if metadata_streams:
                    streams_to_parse = metadata_streams
                    self.log(
                        f"Asset Metadata потоков: {len(metadata_streams)}"
                    )
                else:
                    streams_to_parse = streams
                    self.log(
                        "Asset Metadata не найден, используется legacy-поиск "
                        "только абсолютных путей"
                    )

                for stream in streams_to_parse:
                    try:
                        data = ole.openstream(stream).read()
                        add_paths(data, include_relative=bool(metadata_streams))
                    except Exception:
                        pass
                ole.close()
            else:
                self.log("Прямой парсер...")
                with open(filepath, 'rb') as f:
                    data = f.read()
                add_paths(data)
        except Exception as e:
            self.log(f"Ошибка: {e}")

        # Parse nested .max XRefs to collect their resources and further XRefs.
        for xref_path in sorted(xrefs, key=str.casefold):
            if os.path.splitext(xref_path)[1].lower() == '.max' and os.path.exists(xref_path):
                self.parse_scene_recursive(xref_path, result, visited, root_scene)


class Archiver:
    
    def __init__(self, log_func=None):
        self.log_func = log_func
        self.parser = MaxParser(log_func)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[logging.FileHandler('max_archiver.log', encoding='utf-8')]
        )
    
    def log(self, msg):
        logging.info(msg)
        if self.log_func:
            self.log_func(msg)

    @staticmethod
    def make_archive_name(filename, organize, used_names, category=None):
        if organize:
            root = "xrefs/" if category == 'xref' else "maps/"
        else:
            root = ""
        arcname = f"{root}{filename}"

        if arcname.lower() not in used_names:
            return arcname

        # Preserve the original filename. Each repeated collision is placed at
        # the next folder level inside the resources root.
        counter = 1
        while True:
            arcname = f"{root}duplicates_{counter}/{filename}"
            if arcname.lower() not in used_names:
                return arcname
            counter += 1
    
    def create(self, scene_path, archive_path, categories, organize=True, progress_func=None):
        
        stats = {'added': 0, 'resources': 0, 'missing': 0, 'errors': []}
        
        try:
            self.log("=" * 40)
            self.log(f"Архивация: {os.path.basename(scene_path)}")
            
            if progress_func:
                progress_func(10)
            
            # Парсинг
            paths_dict = self.parser.parse(scene_path)
            
            if progress_func:
                progress_func(30)
            
            # Проверка файлов
            found = defaultdict(set)
            missing = defaultdict(set)
            archived = defaultdict(set)
            
            for cat, paths in paths_dict.items():
                for p in paths:
                    if os.path.exists(p):
                        found[cat].add(p)
                    elif categories.get(cat, False):
                        missing[cat].add(p)
                        stats['missing'] += 1
            
            if progress_func:
                progress_func(40)
            
            # Создание архива
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                
                # Сцена
                scene_name = os.path.basename(scene_path)
                zf.write(scene_path, scene_name)
                stats['added'] += 1
                self.log(f"+ {scene_name}")
                
                used_names = {scene_name.lower()}
                
                # Ресурсы
                files_list = []
                for cat, paths in sorted(found.items()):
                    if categories.get(cat, False):
                        for p in sorted(paths, key=str.casefold):
                            files_list.append((p, cat))
                
                total = len(files_list)
                
                for idx, (fpath, cat) in enumerate(files_list):
                    try:
                        fname = os.path.basename(fpath)
                        arcname = self.make_archive_name(fname, organize, used_names, cat)
                        
                        zf.write(fpath, arcname)
                        used_names.add(arcname.lower())
                        archived[cat].add(fpath)
                        stats['added'] += 1
                        stats['resources'] += 1
                        
                        self.log(f"+ {arcname}")
                        
                    except Exception as e:
                        stats['errors'].append({'path': fpath, 'message': str(e)})
                        self.log(f"! Ошибка: {fpath}: {e}")
                    
                    if progress_func and total > 0:
                        progress_func(40 + int(idx / total * 55))
                
                # Отчёт
                report = self.make_report(
                    scene_path, archive_path, archived, missing, categories,
                    stats['errors'], stats['resources']
                )
                zf.writestr("_report.txt", report.encode('utf-8'))
                stats['added'] += 1
            
            if progress_func:
                progress_func(100)
            
            size_mb = os.path.getsize(archive_path) / 1024 / 1024
            self.log(
                f"Готово! Ресурсов: {stats['resources']}, "
                f"файлов в архиве: {stats['added']}, Размер: {size_mb:.1f} MB"
            )

            if stats['missing'] or stats['errors']:
                self.log(
                    f"ВНИМАНИЕ! Отсутствует: {stats['missing']}, "
                    f"ошибок добавления: {len(stats['errors'])}"
                )
            
            return True, stats['added'], size_mb, stats
            
        except Exception as e:
            self.log(f"ОШИБКА: {e}")
            stats['errors'].append({'path': archive_path, 'message': str(e)})
            return False, 0, 0, stats
    
    def make_report(
        self, scene, archive, archived, missing, categories, errors, resource_count
    ):
        lines = [
            "=" * 50,
            "ОТЧЁТ АРХИВАЦИИ",
            "=" * 50,
            "",
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Сцена: {scene}",
            f"Архив: {archive}",
            f"Ресурсов добавлено: {resource_count}",
            "",
            "Автор: @RomanCG",
            "GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2",
            "Telegram: https://t.me/Romak04",
            "Email: romancg@yandex.ru",
            "",
            "-" * 50,
            "ДОБАВЛЕНО:",
        ]
        
        for cat, paths in archived.items():
            if categories.get(cat, False) and paths:
                lines.append(f"\n[{cat.upper()}]")
                for p in sorted(paths):
                    lines.append(f"  {os.path.basename(p)}")
        
        stats_missing = sum(len(paths) for paths in missing.values())
        if stats_missing:
            lines.append("\n" + "-" * 50)
            lines.append(f"ОТСУТСТВУЕТ ({stats_missing}):")
            for cat, paths in missing.items():
                if categories.get(cat, False) and paths:
                    lines.append(f"\n[{cat.upper()}]")
                    for p in sorted(paths):
                        lines.append(f"  {p}")

        if errors:
            lines.append("\n" + "-" * 50)
            lines.append(f"ОШИБКИ ДОБАВЛЕНИЯ ({len(errors)}):")
            for error in errors:
                lines.append(f"  {error['path']}")
                lines.append(f"    {error['message']}")
        
        return '\n'.join(lines)


class App:

    APP_NAME = "Max Scene Packager"
    VERSION = "2.7"
    GITHUB = "https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2"
    TELEGRAM = "https://t.me/Romak04"
    EMAIL = "romancg@yandex.ru"

    COLORS = {
        'background': '#17181A',
        'panel': '#1F2124',
        'surface': '#282C31',
        'text': '#F3F4F6',
        'secondary': '#A0A4AA',
        'disabled': '#6B7280',
        'border': '#34383D',
        'divider': '#2B2F34',
        'accent': '#4D8DFF',
        'accent_hover': '#5795FF',
        'accent_pressed': '#4383F5',
        'success': '#22C55E',
        'warning': '#F59E0B',
        'error': '#EF4444',
    }
    
    def __init__(self, root):
        self.root = root
        self.root.title(f"{self.APP_NAME} v{self.VERSION}")
        self.root.geometry("960x760")
        self.root.minsize(840, 680)
        self.root.configure(bg=self.COLORS['background'])
        self.set_window_icon()

        available_fonts = set(tkfont.families(self.root))
        self.font_family = 'Inter' if 'Inter' in available_fonts else 'Segoe UI'
        self.configure_styles()
        
        self.scene_var = tk.StringVar()
        self.archive_var = tk.StringVar()
        self.scene_var.trace_add('write', self.on_scene_path_changed)
        self.organize_var = tk.BooleanVar(value=True)
        
        self.cat_vars = {
            'texture': tk.BooleanVar(value=True),
            'ies': tk.BooleanVar(value=True),
            'proxy': tk.BooleanVar(value=True),
            'cache': tk.BooleanVar(value=True),
            'audio': tk.BooleanVar(value=True),
            'lut': tk.BooleanVar(value=True),
            'xref': tk.BooleanVar(value=True),
        }
        
        self.archiver = Archiver(self.log)
        self.build_ui()

    @staticmethod
    def resource_path(relative_path):
        base_path = getattr(
            sys,
            '_MEIPASS',
            os.path.dirname(os.path.abspath(__file__)),
        )
        return os.path.join(base_path, relative_path)

    def set_window_icon(self):
        png_path = self.resource_path(os.path.join('assets', 'app-icon.png'))
        ico_path = self.resource_path(os.path.join('assets', 'app-icon.ico'))

        try:
            self._window_icon = tk.PhotoImage(file=png_path)
            self.root.iconphoto(True, self._window_icon)
        except (tk.TclError, OSError):
            self._window_icon = None

        try:
            self.root.iconbitmap(default=ico_path)
        except (tk.TclError, OSError):
            pass

    def configure_styles(self):
        colors = self.COLORS
        style = ttk.Style(self.root)
        style.theme_use('clam')

        style.configure('.',
                        background=colors['background'],
                        foreground=colors['text'],
                        font=(self.font_family, 10))
        style.configure('App.TFrame', background=colors['background'])
        style.configure('Panel.TFrame', background=colors['panel'])
        style.configure('Surface.TFrame', background=colors['surface'])

        style.configure('TLabel',
                        background=colors['background'],
                        foreground=colors['text'],
                        font=(self.font_family, 10))
        style.configure('Panel.TLabel', background=colors['panel'])
        style.configure('Title.TLabel',
                        background=colors['background'],
                        foreground=colors['text'],
                        font=(self.font_family, 15, 'bold'))
        style.configure('Section.TLabel',
                        background=colors['panel'],
                        foreground=colors['text'],
                        font=(self.font_family, 12))
        style.configure('Body.TLabel',
                        background=colors['panel'],
                        foreground=colors['text'],
                        font=(self.font_family, 10))
        style.configure('Muted.TLabel',
                        background=colors['background'],
                        foreground=colors['secondary'],
                        font=(self.font_family, 9))
        style.configure('PanelMuted.TLabel',
                        background=colors['panel'],
                        foreground=colors['secondary'],
                        font=(self.font_family, 9))
        style.configure('Status.TLabel',
                        background=colors['background'],
                        foreground=colors['secondary'],
                        font=(self.font_family, 9))

        style.configure('TEntry',
                        fieldbackground=colors['surface'],
                        foreground=colors['text'],
                        insertcolor=colors['text'],
                        bordercolor=colors['border'],
                        lightcolor=colors['border'],
                        darkcolor=colors['border'],
                        padding=(12, 9),
                        relief='flat')
        style.map('TEntry',
                  bordercolor=[('focus', colors['accent'])],
                  lightcolor=[('focus', colors['accent'])],
                  darkcolor=[('focus', colors['accent'])],
                  foreground=[('disabled', colors['disabled'])])

        style.configure('Secondary.TButton',
                        background=colors['surface'],
                        foreground=colors['text'],
                        bordercolor=colors['border'],
                        lightcolor=colors['border'],
                        darkcolor=colors['border'],
                        padding=(16, 9),
                        font=(self.font_family, 10),
                        relief='flat')
        style.map('Secondary.TButton',
                  background=[('pressed', '#22262A'), ('active', '#2E3338')],
                  bordercolor=[('focus', colors['accent']), ('active', '#41464D')],
                  foreground=[('disabled', colors['disabled'])])

        style.configure('Primary.TButton',
                        background=colors['accent'],
                        foreground='#FFFFFF',
                        bordercolor=colors['accent'],
                        lightcolor=colors['accent'],
                        darkcolor=colors['accent'],
                        padding=(20, 10),
                        font=(self.font_family, 10),
                        relief='flat')
        style.map('Primary.TButton',
                  background=[('pressed', colors['accent_pressed']),
                              ('active', colors['accent_hover']),
                              ('disabled', '#315A9E')],
                  bordercolor=[('pressed', colors['accent_pressed']),
                               ('active', colors['accent_hover']),
                               ('focus', '#86B0FF')],
                  foreground=[('disabled', '#A8BBD9')])

        style.configure('Quiet.TButton',
                        background=colors['panel'],
                        foreground=colors['secondary'],
                        borderwidth=0,
                        padding=(8, 6),
                        font=(self.font_family, 9),
                        relief='flat')
        style.map('Quiet.TButton',
                  background=[('pressed', colors['panel']), ('active', colors['surface'])],
                  foreground=[('active', colors['text']), ('disabled', colors['disabled'])])

        style.configure('Panel.TCheckbutton',
                        background=colors['panel'],
                        foreground=colors['text'],
                        font=(self.font_family, 10),
                        padding=(0, 3),
                        indicatorbackground=colors['surface'],
                        indicatorforeground='#FFFFFF',
                        bordercolor=colors['border'])
        style.map('Panel.TCheckbutton',
                  background=[('active', colors['panel'])],
                  foreground=[('disabled', colors['disabled'])],
                  indicatorbackground=[('selected', colors['accent']),
                                       ('active', '#30353A')],
                  indicatorforeground=[('selected', '#FFFFFF')],
                  bordercolor=[('focus', colors['accent'])])

        style.configure('Industrial.Horizontal.TProgressbar',
                        background=colors['accent'],
                        troughcolor=colors['surface'],
                        bordercolor=colors['surface'],
                        lightcolor=colors['accent'],
                        darkcolor=colors['accent'],
                        thickness=4)

        style.configure('TSeparator', background=colors['divider'])
    
    def build_ui(self):
        colors = self.COLORS
        main = ttk.Frame(self.root, padding=(24, 20, 24, 12), style='App.TFrame')
        main.pack(fill='both', expand=True)

        header = ttk.Frame(main, style='App.TFrame')
        header.pack(fill='x', pady=(0, 16))
        ttk.Label(header, text=self.APP_NAME, style='Title.TLabel').pack(anchor='w')
        ttk.Label(
            header,
            text="Упаковка сцен 3ds Max и внешних ресурсов",
            style='Muted.TLabel',
        ).pack(anchor='w', pady=(4, 0))

        workspace = ttk.Frame(main, style='App.TFrame')
        workspace.pack(fill='x', pady=(0, 16))
        workspace.columnconfigure(0, weight=3)
        workspace.columnconfigure(1, weight=2)
        workspace.rowconfigure(0, weight=1)

        file_card = ttk.Frame(workspace, padding=(20, 16), style='Panel.TFrame')
        file_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        file_card.columnconfigure(1, weight=1)
        ttk.Label(file_card, text="Файлы", style='Section.TLabel').grid(
            row=0, column=0, columnspan=3, sticky='w'
        )
        ttk.Label(
            file_card,
            text="Выберите .max-сцену и путь для ZIP-архива.",
            style='PanelMuted.TLabel',
        ).grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 16))

        ttk.Label(file_card, text="Сцена", style='Body.TLabel').grid(
            row=2, column=0, sticky='w', padx=(0, 16)
        )
        scene_entry = ttk.Entry(file_card, textvariable=self.scene_var)
        scene_entry.grid(row=2, column=1, sticky='ew')
        ttk.Button(
            file_card,
            text="Обзор",
            command=self.browse_scene,
            style='Secondary.TButton',
        ).grid(row=2, column=2, padx=(12, 0))

        ttk.Label(file_card, text="Архив", style='Body.TLabel').grid(
            row=3, column=0, sticky='w', padx=(0, 16), pady=(12, 0)
        )
        archive_entry = ttk.Entry(file_card, textvariable=self.archive_var)
        archive_entry.grid(row=3, column=1, sticky='ew', pady=(12, 0))
        ttk.Button(
            file_card,
            text="Обзор",
            command=self.browse_archive,
            style='Secondary.TButton',
        ).grid(row=3, column=2, padx=(12, 0), pady=(12, 0))

        options_card = ttk.Frame(workspace, padding=(20, 16), style='Panel.TFrame')
        options_card.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        options_card.columnconfigure(0, weight=1)
        options_card.columnconfigure(1, weight=1)
        ttk.Label(options_card, text="Состав архива", style='Section.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w'
        )
        ttk.Label(
            options_card,
            text="Выберите категории зависимостей.",
            style='PanelMuted.TLabel',
            wraplength=300,
        ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 10))

        labels = {'texture': 'Текстуры', 'ies': 'IES', 'proxy': 'Прокси',
                 'cache': 'Кэш', 'audio': 'Аудио', 'lut': 'LUT', 'xref': 'XRef'}

        for i, (key, label) in enumerate(labels.items()):
            ttk.Checkbutton(
                options_card,
                text=label,
                variable=self.cat_vars[key],
                style='Panel.TCheckbutton',
            ).grid(row=2 + i // 2, column=i % 2, sticky='w', padx=(0, 12), pady=1)

        ttk.Separator(options_card).grid(
            row=6, column=0, columnspan=2, sticky='ew', pady=(8, 8)
        )
        ttk.Checkbutton(
            options_card,
            text="Структура maps/ и xrefs/",
            variable=self.organize_var,
            style='Panel.TCheckbutton',
        ).grid(row=7, column=0, columnspan=2, sticky='w')

        controls = ttk.Frame(main, style='App.TFrame')
        controls.pack(fill='x', pady=(0, 12))
        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(controls, textvariable=self.status_var, style='Status.TLabel').pack(
            side='left'
        )
        self.btn_archive = ttk.Button(
            controls,
            text="Создать архив",
            command=self.start_archive,
            style='Primary.TButton',
        )
        self.btn_archive.pack(side='right')
        self.btn_analyze = ttk.Button(
            controls,
            text="Анализировать",
            command=self.start_analyze,
            style='Secondary.TButton',
        )
        self.btn_analyze.pack(side='right', padx=(0, 8))

        self.progress = ttk.Progressbar(
            main,
            mode='determinate',
            style='Industrial.Horizontal.TProgressbar',
        )
        self.progress.pack(fill='x', pady=(0, 16), ipady=1)

        log_card = ttk.Frame(main, padding=(20, 14, 20, 18), style='Panel.TFrame')
        log_card.pack(fill='both', expand=True)
        log_header = ttk.Frame(log_card, style='Panel.TFrame')
        log_header.pack(fill='x', pady=(0, 10))
        ttk.Label(log_header, text="Журнал операций", style='Section.TLabel').pack(
            side='left'
        )
        ttk.Button(
            log_header,
            text="Очистить",
            command=self.clear_log,
            style='Quiet.TButton',
        ).pack(side='right')

        self.log_text = scrolledtext.ScrolledText(
            log_card,
            height=4,
            bg=colors['background'],
            fg=colors['secondary'],
            insertbackground=colors['text'],
            selectbackground=colors['accent'],
            selectforeground='#FFFFFF',
            font=(self.font_family, 9),
            relief='flat',
            borderwidth=0,
            padx=12,
            pady=10,
            wrap='word',
        )
        self.log_text.pack(fill='both', expand=True)
        self.log_text.tag_configure('timestamp', foreground=colors['disabled'])
        self.log_text.tag_configure('success', foreground=colors['success'])
        self.log_text.tag_configure('warning', foreground=colors['warning'])
        self.log_text.tag_configure('error', foreground=colors['error'])

        footer = ttk.Frame(main, style='App.TFrame')
        footer.pack(fill='x', pady=(12, 0))
        ttk.Label(
            footer,
            text=f"v{self.VERSION}  ·  @RomanCG",
            style='Muted.TLabel',
        ).pack(side='left')
        links = ttk.Frame(footer, style='App.TFrame')
        links.pack(side='right')
        self.make_link(links, "GitHub", self.GITHUB).pack(side='left', padx=(0, 16))
        self.make_link(links, "Telegram", self.TELEGRAM).pack(side='left', padx=(0, 16))
        self.make_link(links, "Email", f"mailto:{self.EMAIL}").pack(side='left')

    def make_link(self, parent, text, url):
        link = tk.Label(
            parent,
            text=text,
            bg=self.COLORS['background'],
            fg=self.COLORS['secondary'],
            activebackground=self.COLORS['background'],
            activeforeground=self.COLORS['text'],
            cursor='hand2',
            font=(self.font_family, 9),
            padx=0,
            pady=0,
        )
        link.bind('<Enter>', lambda _event: link.configure(fg=self.COLORS['text']))
        link.bind('<Leave>', lambda _event: link.configure(fg=self.COLORS['secondary']))
        link.bind('<Button-1>', lambda _event: webbrowser.open(url))
        return link
    
    def browse_scene(self):
        f = filedialog.askopenfilename(filetypes=[("3ds Max", "*.max")])
        if f:
            self.scene_var.set(f)

    @staticmethod
    def default_archive_path(scene_path):
        scene_path = scene_path.strip()
        if not scene_path:
            return ""
        return os.path.splitext(scene_path)[0] + "_archive.zip"

    def on_scene_path_changed(self, *_args):
        self.archive_var.set(self.default_archive_path(self.scene_var.get()))
    
    def browse_archive(self):
        f = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if f:
            self.archive_var.set(f)
    
    def log(self, msg):
        def update():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert('end', f"[{ts}] ", 'timestamp')
            lowered = msg.lower()
            if 'ошиб' in lowered or msg.lstrip().startswith('✗'):
                tag = 'error'
            elif 'предуп' in lowered or 'отсутств' in lowered:
                tag = 'warning'
            elif 'готово' in lowered or msg.lstrip().startswith('+'):
                tag = 'success'
            else:
                tag = None
            self.log_text.insert('end', f"{msg}\n", tag)
            self.log_text.see('end')
        self.root.after(0, update)
    
    def clear_log(self):
        self.log_text.delete('1.0', 'end')
    
    def set_progress(self, val):
        self.root.after(0, lambda: self.progress.configure(value=val))
    
    def set_status(self, msg):
        self.root.after(0, lambda: self.status_var.set(msg))
    
    def set_enabled(self, enabled):
        state = 'normal' if enabled else 'disabled'
        self.root.after(0, lambda: (
            self.btn_archive.configure(state=state),
            self.btn_analyze.configure(state=state),
        ))
    
    def start_analyze(self):
        if not self.scene_var.get():
            messagebox.showerror("Ошибка", "Выберите сцену!")
            return
        if not os.path.exists(self.scene_var.get()):
            messagebox.showerror("Ошибка", "Файл не найден!")
            return
        
        threading.Thread(target=self.do_analyze, daemon=True).start()
    
    def do_analyze(self):
        self.set_enabled(False)
        self.set_status("Анализ...")
        
        try:
            paths = self.archiver.parser.parse(self.scene_var.get())
            
            self.log("\n" + "=" * 30)
            self.log("РЕЗУЛЬТАТЫ")
            
            total = 0
            for cat, pset in sorted(paths.items()):
                if pset:
                    total += len(pset)
                    self.log(f"\n[{cat.upper()}] - {len(pset)}:")
                    for p in sorted(pset)[:10]:
                        exists = "✓" if os.path.exists(p) else "✗"
                        self.log(f"  {exists} {os.path.basename(p)}")
                    if len(pset) > 10:
                        self.log(f"  ... ещё {len(pset) - 10}")
            
            self.log(f"\nИтого: {total}")
            self.set_status(f"Найдено: {total}")
        except Exception as e:
            self.log(f"Ошибка: {e}")
            self.set_status("Ошибка")
        finally:
            self.set_enabled(True)
            self.set_progress(100)
    
    def start_archive(self):
        if not self.scene_var.get():
            messagebox.showerror("Ошибка", "Выберите сцену!")
            return
        if not os.path.exists(self.scene_var.get()):
            messagebox.showerror("Ошибка", "Файл не найден!")
            return
        if not self.archive_var.get():
            messagebox.showerror("Ошибка", "Укажите архив!")
            return
        
        threading.Thread(target=self.do_archive, daemon=True).start()
    
    def do_archive(self):
        self.set_enabled(False)
        self.set_status("Архивация...")
        self.set_progress(0)
        
        try:
            cats = {k: v.get() for k, v in self.cat_vars.items()}
            
            ok, count, size, stats = self.archiver.create(
                self.scene_var.get(),
                self.archive_var.get(),
                cats,
                self.organize_var.get(),
                self.set_progress
            )
            
            if ok:
                if stats['missing'] or stats['errors']:
                    warning_lines = [
                        "Архив создан, но не все ресурсы удалось добавить.",
                        "",
                        f"Ресурсов добавлено: {stats['resources']}",
                        f"Отсутствует файлов: {stats['missing']}",
                        f"Ошибок добавления: {len(stats['errors'])}",
                        "",
                        "Подробности находятся в _report.txt внутри архива.",
                    ]
                    self.set_status(
                        f"Готово с предупреждениями: отсутствует {stats['missing']}, "
                        f"ошибок {len(stats['errors'])}"
                    )
                    messagebox.showwarning(
                        "Архив создан с предупреждениями", "\n".join(warning_lines)
                    )
                else:
                    self.set_status(
                        f"Готово: {stats['resources']} ресурсов, {size:.1f} MB"
                    )
                    messagebox.showinfo(
                        "Успех",
                        f"Архив создан!\n\n"
                        f"Ресурсов: {stats['resources']}\n"
                        f"Файлов в архиве: {count}\n"
                        f"Размер: {size:.1f} MB"
                    )
            else:
                self.set_status("Ошибка")
                messagebox.showerror("Ошибка", "Не удалось создать архив")
        except Exception as e:
            self.set_status("Ошибка")
            self.log(f"ОШИБКА: {e}")
            messagebox.showerror("Ошибка", str(e))
        finally:
            self.set_enabled(True)


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
