#!/usr/bin/env python3
"""
3ds Max Scene Archiver v2.1
Исправлены: дублирование сцены, лишние файлы
"""

import os
import sys
import re
import zipfile
import logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
from typing import Set, Dict, Tuple, Optional

# Попытка импорта olefile
try:
    import olefile
    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False
    print("Рекомендуется: pip install olefile")


class PathExtractor:
    """Извлечение путей из бинарных данных"""
    
    TEXTURE_EXTENSIONS = frozenset({
        '.jpg', '.jpeg', '.png', '.tga', '.tif', '.tiff', '.bmp', 
        '.hdr', '.exr', '.psd', '.dds', '.gif', '.webp',
        '.tx', '.tex', '.sgi', '.rgb', '.rgba', '.pic'
    })
    
    IES_EXTENSIONS = frozenset({'.ies'})
    
    PROXY_EXTENSIONS = frozenset({
        '.vrmesh', '.vrscene', '.vrlmap', '.vrmap',
        '.cgeo', '.cproxy',
        '.abc', '.obj', '.fbx',
        '.rs', '.rstex',
    })
    
    # ВАЖНО: исключаем .max из автоматического сбора
    SCENE_EXTENSIONS = frozenset({'.max', '.chr', '.cat'})
    
    CACHE_EXTENSIONS = frozenset({
        '.pc2', '.mdd', '.mc',
        '.xml', '.bin', '.bif',
        '.tyc', '.tyflow',
        '.fxd', '.fxa',
    })
    
    AUDIO_EXTENSIONS = frozenset({'.wav', '.mp3', '.aif', '.aiff', '.ogg'})
    
    # Ресурсы которые ищем (БЕЗ .max файлов - они обрабатываются отдельно)
    RESOURCE_EXTENSIONS = (
        TEXTURE_EXTENSIONS | IES_EXTENSIONS | PROXY_EXTENSIONS | 
        CACHE_EXTENSIONS | AUDIO_EXTENSIONS
    )
    
    # Все расширения для парсинга
    ALL_EXTENSIONS = RESOURCE_EXTENSIONS | SCENE_EXTENSIONS
    
    def __init__(self):
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Компилирует регулярные выражения"""
        ext_pattern = b'|'.join(ext[1:].encode() for ext in self.ALL_EXTENSIONS)
        
        self.simple_ascii_pattern = re.compile(
            rb'([A-Za-z]:[\\\/][^\x00-\x1f"*<>|]{5,260}?'
            rb'\.(?:' + ext_pattern + rb'))',
            re.IGNORECASE
        )
    
    def extract_paths_from_bytes(self, data: bytes, 
                                  exclude_extensions: frozenset = None) -> Set[str]:
        """Извлекает пути, исключая указанные расширения"""
        paths = set()
        
        if exclude_extensions is None:
            exclude_extensions = frozenset()
        
        # ASCII пути
        paths.update(self._extract_ascii_paths(data, exclude_extensions))
        
        # UTF-16 LE пути
        paths.update(self._extract_utf16_paths(data, exclude_extensions))
        
        return paths
    
    def _extract_ascii_paths(self, data: bytes, 
                              exclude_extensions: frozenset) -> Set[str]:
        """Извлекает ASCII пути"""
        paths = set()
        
        try:
            for match in self.simple_ascii_pattern.finditer(data):
                try:
                    path = match.group(0).decode('ascii', errors='ignore')
                    path = self._normalize_path(path)
                    
                    if path and self._is_valid_path(path, exclude_extensions):
                        paths.add(path)
                except:
                    pass
        except Exception as e:
            logging.debug(f"Ошибка ASCII: {e}")
        
        return paths
    
    def _extract_utf16_paths(self, data: bytes, 
                              exclude_extensions: frozenset) -> Set[str]:
        """Извлекает UTF-16 LE пути"""
        paths = set()
        
        drive_letters = b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
        
        i = 0
        data_len = len(data)
        
        while i < data_len - 10:
            if (i + 5 < data_len and 
                data[i] in drive_letters and 
                data[i+1] == 0 and 
                data[i+2] == ord(':') and 
                data[i+3] == 0 and
                data[i+4] == ord('\\') and
                data[i+5] == 0):
                
                path_bytes = bytearray()
                j = i
                
                while j < min(i + 520, data_len - 1):
                    char = data[j]
                    next_byte = data[j + 1] if j + 1 < data_len else 0
                    
                    if char == 0 and next_byte == 0:
                        break
                    
                    if next_byte == 0 and char < 32:
                        break
                    
                    path_bytes.append(char)
                    path_bytes.append(next_byte)
                    j += 2
                
                try:
                    path = bytes(path_bytes).decode('utf-16-le', errors='ignore').strip('\x00')
                    path = self._normalize_path(path)
                    
                    if path and self._is_valid_path(path, exclude_extensions):
                        paths.add(path)
                except:
                    pass
                
                i = j
            else:
                i += 1
        
        return paths
    
    def _normalize_path(self, path: str) -> str:
        """Нормализует путь"""
        if not path:
            return ""
        
        path = path.strip('\x00\r\n\t ')
        path = path.replace('/', '\\')
        
        while '\\\\\\' in path:
            path = path.replace('\\\\\\', '\\\\')
        
        path = re.sub(r'[\x00-\x1f]', '', path)
        
        return path
    
    def _is_valid_path(self, path: str, exclude_extensions: frozenset) -> bool:
        """Проверяет валидность пути"""
        if not path or len(path) < 5:
            return False
        
        if not re.match(r'^[A-Za-z]:\\', path):
            return False
        
        ext = os.path.splitext(path)[1].lower()
        
        # Проверяем исключения
        if ext in exclude_extensions:
            return False
        
        if ext not in self.ALL_EXTENSIONS:
            return False
        
        if any(c in path for c in ['*', '?', '"', '<', '>', '|']):
            return False
        
        return True
    
    def categorize_path(self, path: str) -> str:
        """Категория файла"""
        ext = os.path.splitext(path)[1].lower()
        
        if ext in self.TEXTURE_EXTENSIONS:
            return 'texture'
        elif ext in self.IES_EXTENSIONS:
            return 'ies'
        elif ext in self.PROXY_EXTENSIONS:
            return 'proxy'
        elif ext in self.SCENE_EXTENSIONS:
            return 'scene'
        elif ext in self.CACHE_EXTENSIONS:
            return 'cache'
        elif ext in self.AUDIO_EXTENSIONS:
            return 'audio'
        else:
            return 'other'


class MaxFileParser:
    """Парсер .max файлов"""
    
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.path_extractor = PathExtractor()
    
    def log(self, message: str):
        logging.info(message)
        if self.log_callback:
            self.log_callback(message)
    
    def parse(self, max_file_path: str, 
              exclude_scene_files: bool = True) -> Dict[str, Set[str]]:
        """
        Парсит .max файл
        
        Args:
            max_file_path: путь к файлу сцены
            exclude_scene_files: исключать ли .max файлы из результатов
        """
        result = defaultdict(set)
        
        if not os.path.exists(max_file_path):
            self.log(f"Файл не найден: {max_file_path}")
            return dict(result)
        
        # Определяем что исключать
        exclude_ext = frozenset()
        if exclude_scene_files:
            exclude_ext = PathExtractor.SCENE_EXTENSIONS
        
        file_size = os.path.getsize(max_file_path)
        self.log(f"Анализ: {os.path.basename(max_file_path)} ({file_size / 1024 / 1024:.1f} MB)")
        
        try:
            if HAS_OLEFILE:
                result = self._parse_with_olefile(max_file_path, exclude_ext)
            else:
                result = self._parse_raw(max_file_path, exclude_ext)
        except Exception as e:
            self.log(f"Ошибка парсинга: {e}")
            try:
                result = self._parse_raw(max_file_path, exclude_ext)
            except Exception as e2:
                self.log(f"Резервный метод тоже не сработал: {e2}")
        
        return dict(result)
    
    def _parse_with_olefile(self, max_file_path: str, 
                            exclude_ext: frozenset) -> Dict[str, Set[str]]:
        """Парсинг через olefile"""
        result = defaultdict(set)
        
        self.log("Используется OLE парсер...")
        
        ole = olefile.OleFileIO(max_file_path)
        streams = ole.listdir()
        self.log(f"OLE потоков: {len(streams)}")
        
        for stream_path in streams:
            try:
                data = ole.openstream(stream_path).read()
                paths = self.path_extractor.extract_paths_from_bytes(data, exclude_ext)
                
                for path in paths:
                    category = self.path_extractor.categorize_path(path)
                    result[category].add(path)
            except:
                pass
        
        ole.close()
        
        total = sum(len(v) for v in result.values())
        self.log(f"Найдено путей: {total}")
        
        return result
    
    def _parse_raw(self, max_file_path: str, 
                   exclude_ext: frozenset) -> Dict[str, Set[str]]:
        """Прямой парсинг"""
        result = defaultdict(set)
        
        self.log("Используется прямой парсер...")
        
        chunk_size = 10 * 1024 * 1024
        overlap = 1024
        file_size = os.path.getsize(max_file_path)
        
        with open(max_file_path, 'rb') as f:
            offset = 0
            prev_tail = b''
            
            while offset < file_size:
                chunk = prev_tail + f.read(chunk_size)
                
                if not chunk:
                    break
                
                paths = self.path_extractor.extract_paths_from_bytes(chunk, exclude_ext)
                
                for path in paths:
                    category = self.path_extractor.categorize_path(path)
                    result[category].add(path)
                
                if len(chunk) > overlap:
                    prev_tail = chunk[-overlap:]
                else:
                    prev_tail = b''
                
                offset += chunk_size
        
        total = sum(len(v) for v in result.values())
        self.log(f"Найдено путей: {total}")
        
        return result


class SceneArchiver:
    """Архиватор сцен"""
    
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.parser = MaxFileParser(log_callback)
        self.setup_logging()
    
    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('max_archiver.log', encoding='utf-8'),
            ]
        )
    
    def log(self, message: str):
        logging.info(message)
        if self.log_callback:
            self.log_callback(message)
    
    def verify_paths(self, paths_by_category: Dict[str, Set[str]]) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
        """Проверяет существование файлов"""
        found = defaultdict(set)
        missing = defaultdict(set)
        
        for category, paths in paths_by_category.items():
            for path in paths:
                if os.path.exists(path):
                    found[category].add(path)
                else:
                    missing[category].add(path)
        
        return dict(found), dict(missing)
    
    def create_archive(self, 
                       max_file_path: str,
                       archive_path: str,
                       include_categories: Dict[str, bool] = None,
                       search_additional: bool = False,  # ОТКЛЮЧЕНО по умолчанию
                       organize_structure: bool = True,
                       progress_callback=None) -> Tuple[bool, int, float, Dict]:
        """Создаёт архив"""
        
        if include_categories is None:
            include_categories = {
                'texture': True,
                'ies': True,
                'proxy': True,
                'cache': True,
                'audio': True,
                'other': True,
            }
        
        stats = {
            'found': defaultdict(int),
            'missing': defaultdict(int),
            'added': defaultdict(int),
            'errors': []
        }
        
        try:
            self.log("=" * 50)
            self.log(f"Архивация: {os.path.basename(max_file_path)}")
            self.log("=" * 50)
            
            if progress_callback:
                progress_callback(5)
            
            # Парсим сцену (исключая .max файлы)
            self.log("\n[1/3] Анализ сцены...")
            paths_by_category = self.parser.parse(max_file_path, exclude_scene_files=True)
            
            if progress_callback:
                progress_callback(30)
            
            # Проверяем существование
            self.log("\n[2/3] Проверка файлов...")
            found_paths, missing_paths = self.verify_paths(paths_by_category)
            
            for category, paths in found_paths.items():
                stats['found'][category] = len(paths)
                self.log(f"  {category}: {len(paths)} файлов")
            
            for category, paths in missing_paths.items():
                stats['missing'][category] = len(paths)
                if paths:
                    self.log(f"  {category}: отсутствует {len(paths)}")
            
            if progress_callback:
                progress_callback(40)
            
            # Создаём архив
            self.log("\n[3/3] Создание архива...")
            
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, 
                                compresslevel=6) as zipf:
                
                added_files = 0
                used_names = set()
                
                # 1. Добавляем ТОЛЬКО основную сцену в корень
                scene_name = os.path.basename(max_file_path)
                zipf.write(max_file_path, scene_name)
                added_files += 1
                used_names.add(scene_name.lower())
                self.log(f"  + {scene_name} (сцена)")
                
                # 2. Собираем ресурсы для добавления
                files_to_add = []
                
                for category, paths in found_paths.items():
                    # Пропускаем сцены - они не должны были попасть в found_paths,
                    # но проверим на всякий случай
                    if category == 'scene':
                        continue
                    
                    if not include_categories.get(category, True):
                        self.log(f"  Пропуск: {category}")
                        continue
                    
                    for path in paths:
                        # Дополнительная проверка: не добавляем .max файлы
                        ext = os.path.splitext(path)[1].lower()
                        if ext in {'.max', '.chr', '.cat'}:
                            continue
                        
                        files_to_add.append((path, category))
                
                total_files = len(files_to_add)
                self.log(f"  Файлов для добавления: {total_files}")
                
                # 3. Добавляем ресурсы
                for idx, (file_path, category) in enumerate(files_to_add):
                    try:
                        filename = os.path.basename(file_path)
                        
                        if organize_structure:
                            # Все ресурсы в папку maps/
                            base_arcname = f"maps/{filename}"
                        else:
                            base_arcname = filename
                        
                        # Обработка конфликтов имён
                        arcname = base_arcname
                        arcname_lower = arcname.lower()
                        
                        if arcname_lower in used_names:
                            base, ext = os.path.splitext(base_arcname)
                            counter = 1
                            while f"{base}_{counter:02d}{ext}".lower() in used_names:
                                counter += 1
                            arcname = f"{base}_{counter:02d}{ext}"
                        
                        used_names.add(arcname.lower())
                        
                        # Добавляем
                        zipf.write(file_path, arcname)
                        added_files += 1
                        stats['added'][category] = stats['added'].get(category, 0) + 1
                        
                        size_kb = os.path.getsize(file_path) / 1024
                        self.log(f"  + {arcname} ({size_kb:.1f} KB)")
                        
                    except Exception as e:
                        stats['errors'].append(f"{file_path}: {e}")
                        self.log(f"  ! Ошибка: {filename} - {e}")
                    
                    if progress_callback and total_files > 0:
                        progress = 40 + (idx / total_files * 55)
                        progress_callback(progress)
                
                # 4. Отчёт
                report = self._create_report(
                    max_file_path, archive_path, stats, 
                    found_paths, missing_paths,
                    include_categories, organize_structure
                )
                zipf.writestr("_archive_report.txt", report.encode('utf-8'))
                added_files += 1
            
            if progress_callback:
                progress_callback(100)
            
            archive_size = os.path.getsize(archive_path) / (1024 * 1024)
            
            self.log("\n" + "=" * 50)
            self.log("ГОТОВО!")
            self.log(f"Файлов: {added_files}")
            self.log(f"Размер: {archive_size:.2f} MB")
            self.log("=" * 50)
            
            return True, added_files, archive_size, dict(stats)
            
        except Exception as e:
            self.log(f"\n!!! ОШИБКА: {e}")
            logging.exception("Ошибка архивации")
            return False, 0, 0, dict(stats)
    
    def _create_report(self, max_file_path: str, archive_path: str,
                       stats: Dict, found_paths: Dict, missing_paths: Dict,
                       include_categories: Dict, organize_structure: bool) -> str:
        """Создаёт отчёт"""
        
        lines = [
            "=" * 60,
            "ОТЧЁТ АРХИВАЦИИ 3DS MAX",
            "=" * 60,
            "",
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Сцена: {max_file_path}",
            f"Архив: {archive_path}",
            "",
            "СТРУКТУРА АРХИВА:",
            f"  Сцена (.max) - в корне архива",
            f"  Ресурсы - {'в папке maps/' if organize_structure else 'в корне'}",
            "",
            "-" * 60,
            "СТАТИСТИКА",
            "-" * 60,
        ]
        
        total_added = sum(stats['added'].values())
        total_missing = sum(stats['missing'].values())
        
        lines.append(f"Добавлено в архив: {total_added + 1}")  # +1 за сцену
        lines.append(f"Отсутствует: {total_missing}")
        
        if found_paths:
            lines.extend(["", "ДОБАВЛЕННЫЕ ФАЙЛЫ:", ""])
            
            for category, paths in sorted(found_paths.items()):
                if category == 'scene':
                    continue
                if paths and include_categories.get(category, True):
                    lines.append(f"[{category.upper()}] - {len(paths)}:")
                    for path in sorted(paths):
                        lines.append(f"  ✓ {os.path.basename(path)}")
                    lines.append("")
        
        if any(missing_paths.values()):
            lines.extend(["", "ОТСУТСТВУЮЩИЕ ФАЙЛЫ:", ""])
            
            for category, paths in sorted(missing_paths.items()):
                if paths:
                    lines.append(f"[{category.upper()}] - {len(paths)}:")
                    for path in sorted(paths):
                        lines.append(f"  ✗ {path}")
                    lines.append("")
        
        return '\n'.join(lines)


class ArchiverGUI:
    """GUI"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("3ds Max Scene Archiver v2.1")
        self.root.geometry("900x750")
        self.root.minsize(800, 650)
        
        self.scene_path = tk.StringVar()
        self.archive_path = tk.StringVar()
        
        self.categories = {
            'texture': tk.BooleanVar(value=True),
            'ies': tk.BooleanVar(value=True),
            'proxy': tk.BooleanVar(value=True),
            'cache': tk.BooleanVar(value=True),
            'audio': tk.BooleanVar(value=True),
            'other': tk.BooleanVar(value=True),
        }
        
        # ОТКЛЮЧЕНО по умолчанию - только файлы из сцены
        self.search_additional = tk.BooleanVar(value=False)
        self.organize_structure = tk.BooleanVar(value=True)
        
        self.is_processing = False
        
        self.setup_ui()
        self.archiver = SceneArchiver(log_callback=self.add_log)
    
    def setup_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.grid(row=0, column=0, sticky='nsew')
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        
        row = 0
        
        # Заголовок
        title = ttk.Label(main, text="3ds Max Scene Archiver v2.1", 
                         font=('Segoe UI', 14, 'bold'))
        title.grid(row=row, column=0, columnspan=3, pady=(0, 5))
        
        row += 1
        subtitle = ttk.Label(main, 
            text="Архивация сцен без запуска 3ds Max",
            font=('Segoe UI', 9), foreground='#666')
        subtitle.grid(row=row, column=0, columnspan=3, pady=(0, 15))
        
        # OLE статус
        row += 1
        if HAS_OLEFILE:
            status_text = "✓ olefile установлен"
            status_color = "green"
        else:
            status_text = "⚠ pip install olefile - для лучших результатов"
            status_color = "orange"
        
        ttk.Label(main, text=status_text, foreground=status_color).grid(
            row=row, column=0, columnspan=3, pady=(0, 15))
        
        # Файл сцены
        row += 1
        ttk.Label(main, text="Сцена:").grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(main, textvariable=self.scene_path, width=70).grid(
            row=row, column=1, sticky='ew', pady=5, padx=5)
        ttk.Button(main, text="Обзор", command=self.browse_scene, width=10).grid(
            row=row, column=2, pady=5)
        
        # Файл архива
        row += 1
        ttk.Label(main, text="Архив:").grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(main, textvariable=self.archive_path, width=70).grid(
            row=row, column=1, sticky='ew', pady=5, padx=5)
        ttk.Button(main, text="Обзор", command=self.browse_archive, width=10).grid(
            row=row, column=2, pady=5)
        
        # Категории
        row += 1
        cat_frame = ttk.LabelFrame(main, text="Типы файлов", padding=10)
        cat_frame.grid(row=row, column=0, columnspan=3, sticky='ew', pady=10)
        
        labels = {
            'texture': 'Текстуры',
            'ies': 'IES',
            'proxy': 'Прокси',
            'cache': 'Кэш',
            'audio': 'Аудио',
            'other': 'Другое',
        }
        
        for idx, (key, label) in enumerate(labels.items()):
            ttk.Checkbutton(cat_frame, text=label, variable=self.categories[key]).grid(
                row=0, column=idx, sticky='w', padx=10)
        
        # Настройки
        row += 1
        opt_frame = ttk.LabelFrame(main, text="Настройки", padding=10)
        opt_frame.grid(row=row, column=0, columnspan=3, sticky='ew', pady=10)
        
        ttk.Checkbutton(opt_frame, 
                       text="Ресурсы в папку maps/ (иначе в корень)",
                       variable=self.organize_structure).grid(row=0, column=0, sticky='w')
        
        # Кнопки
        row += 1
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=15)
        
        self.archive_btn = ttk.Button(btn_frame, text="📦 Создать архив", 
                                     command=self.start_archive, width=18)
        self.archive_btn.pack(side='left', padx=5)
        
        ttk.Button(btn_frame, text="📋 Анализ", 
                  command=self.analyze_only, width=12).pack(side='left', padx=5)
        
        ttk.Button(btn_frame, text="🗑 Очистить", 
                  command=self.clear_log, width=12).pack(side='left', padx=5)
        
        # Прогресс
        row += 1
        self.progress = ttk.Progressbar(main, mode='determinate')
        self.progress.grid(row=row, column=0, columnspan=3, sticky='ew', pady=10)
        
        # Статус
        row += 1
        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(main, textvariable=self.status_var, foreground='#666').grid(
            row=row, column=0, columnspan=3, sticky='w')
        
        # Лог
        row += 1
        ttk.Label(main, text="Журнал:").grid(row=row, column=0, sticky='w', pady=(10, 5))
        
        row += 1
        main.rowconfigure(row, weight=1)
        
        log_frame = ttk.Frame(main)
        log_frame.grid(row=row, column=0, columnspan=3, sticky='nsew')
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=18,
                                                  font=('Consolas', 9))
        self.log_text.grid(row=0, column=0, sticky='nsew')
    
    def browse_scene(self):
        filename = filedialog.askopenfilename(
            title="Выберите сцену",
            filetypes=[("3ds Max", "*.max"), ("Все", "*.*")]
        )
        if filename:
            self.scene_path.set(filename)
            if not self.archive_path.get():
                base = os.path.splitext(filename)[0]
                self.archive_path.set(f"{base}_archive.zip")
    
    def browse_archive(self):
        filename = filedialog.asksaveasfilename(
            title="Сохранить архив",
            defaultextension=".zip",
            filetypes=[("ZIP", "*.zip")]
        )
        if filename:
            self.archive_path.set(filename)
    
    def add_log(self, message: str):
        def update():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert('end', f"[{ts}] {message}\n")
            self.log_text.see('end')
        self.root.after(0, update)
    
    def clear_log(self):
        self.log_text.delete('1.0', 'end')
    
    def update_progress(self, value):
        self.root.after(0, lambda: setattr(self.progress, 'value', value))
    
    def update_status(self, msg):
        self.root.after(0, lambda: self.status_var.set(msg))
    
    def set_processing(self, processing):
        def update():
            self.is_processing = processing
            self.archive_btn.config(
                state='disabled' if processing else 'normal',
                text="⏳ Обработка..." if processing else "📦 Создать архив"
            )
        self.root.after(0, update)
    
    def analyze_only(self):
        if not self.scene_path.get():
            messagebox.showerror("Ошибка", "Выберите сцену!")
            return
        
        if not os.path.exists(self.scene_path.get()):
            messagebox.showerror("Ошибка", "Файл не найден!")
            return
        
        thread = threading.Thread(target=self._analyze_thread, daemon=True)
        thread.start()
    
    def _analyze_thread(self):
        self.set_processing(True)
        self.update_status("Анализ...")
        
        try:
            paths = self.archiver.parser.parse(self.scene_path.get())
            
            self.add_log("\n" + "=" * 40)
            self.add_log("РЕЗУЛЬТАТЫ АНАЛИЗА")
            self.add_log("=" * 40)
            
            total = 0
            for cat, path_set in sorted(paths.items()):
                if not path_set:
                    continue
                    
                total += len(path_set)
                self.add_log(f"\n[{cat.upper()}] - {len(path_set)}:")
                
                for path in sorted(path_set)[:15]:
                    exists = "✓" if os.path.exists(path) else "✗"
                    self.add_log(f"  {exists} {os.path.basename(path)}")
                
                if len(path_set) > 15:
                    self.add_log(f"  ... ещё {len(path_set) - 15}")
            
            self.add_log(f"\nИТОГО: {total}")
            self.update_status(f"Найдено: {total}")
            
        except Exception as e:
            self.add_log(f"Ошибка: {e}")
            self.update_status("Ошибка")
        finally:
            self.set_processing(False)
            self.update_progress(100)
    
    def start_archive(self):
        if not self.scene_path.get():
            messagebox.showerror("Ошибка", "Выберите сцену!")
            return
        
        if not os.path.exists(self.scene_path.get()):
            messagebox.showerror("Ошибка", "Файл не найден!")
            return
        
        if not self.archive_path.get():
            messagebox.showerror("Ошибка", "Укажите путь архива!")
            return
        
        thread = threading.Thread(target=self._archive_thread, daemon=True)
        thread.start()
    
    def _archive_thread(self):
        self.set_processing(True)
        self.update_status("Архивация...")
        self.progress['value'] = 0
        
        try:
            include = {k: v.get() for k, v in self.categories.items()}
            
            ok, count, size, stats = self.archiver.create_archive(
                self.scene_path.get(),
                self.archive_path.get(),
                include_categories=include,
                search_additional=self.search_additional.get(),
                organize_structure=self.organize_structure.get(),
                progress_callback=self.update_progress
            )
            
            if ok:
                self.update_status(f"Готово: {count} файлов, {size:.1f} MB")
                messagebox.showinfo("Успех", 
                    f"Архив создан!\n\nФайлов: {count}\nРазмер: {size:.2f} MB")
            else:
                self.update_status("Ошибка")
                messagebox.showerror("Ошибка", "Не удалось создать архив")
                
        except Exception as e:
            self.update_status("Критическая ошибка")
            self.add_log(f"ОШИБКА: {e}")
            messagebox.showerror("Ошибка", str(e))
        finally:
            self.set_processing(False)


def main():
    root = tk.Tk()
    ArchiverGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
