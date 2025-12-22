#!/usr/bin/env python3
"""
3Ds Max Scene Archiver in ZIP v2.1
Автор: @RomanCG
GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2
Behance: https://www.behance.net/Romancg62fe
"""

import os
import re
import zipfile
import logging
import webbrowser
from datetime import datetime
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading

try:
    import olefile
    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False


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

    
    def extract_paths(self, data, exclude_ext=None):
        if exclude_ext is None:
            exclude_ext = set()
        
        paths = set()
        
        # ASCII
        try:
            pattern = rb'[A-Za-z]:[\\\/][^\x00-\x1f"*<>|]{5,260}\.[A-Za-z0-9]{1,10}'
            for match in re.finditer(pattern, data):
                try:
                    path = match.group(0).decode('ascii', errors='ignore')
                    path = self.normalize(path)
                    if self.is_valid(path, exclude_ext):
                        paths.add(path)
                except:
                    pass
        except:
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
                    except:
                        pass
                    i = j
                else:
                    i += 1
        except:
            pass
        
        return paths
    
    def normalize(self, path):
        if not path:
            return ""
        path = path.strip('\x00\r\n\t ')
        path = path.replace('/', '\\')
        return path
    
    def is_valid(self, path, exclude_ext):
        if not path or len(path) < 5:
            return False
        if not re.match(r'^[A-Za-z]:\\', path):
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext in exclude_ext:
            return False
        if ext not in self.ALL_EXT:
            return False
        return True
    
    def categorize(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in self.TEXTURE_EXT:
            return 'texture'
        elif ext in self.IES_EXT:
            return 'ies'
        elif ext in self.PROXY_EXT:
            return 'proxy'
        elif ext in self.SCENE_EXT:
            return 'scene'
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
    
    def log(self, msg):
        if self.log_func:
            self.log_func(msg)
    
    def parse(self, filepath):
        result = defaultdict(set)
        
        if not os.path.exists(filepath):
            self.log(f"Файл не найден: {filepath}")
            return dict(result)
        
        size_mb = os.path.getsize(filepath) / 1024 / 1024
        self.log(f"Анализ: {os.path.basename(filepath)} ({size_mb:.1f} MB)")
        
        exclude = PathExtractor.SCENE_EXT
        
        try:
            if HAS_OLEFILE:
                self.log("OLE парсер...")
                ole = olefile.OleFileIO(filepath)
                for stream in ole.listdir():
                    try:
                        data = ole.openstream(stream).read()
                        paths = self.extractor.extract_paths(data, exclude)
                        for p in paths:
                            cat = self.extractor.categorize(p)
                            if cat:
                                result[cat].add(p)
                    except:
                        pass
                ole.close()
            else:
                self.log("Прямой парсер...")
                with open(filepath, 'rb') as f:
                    data = f.read()
                paths = self.extractor.extract_paths(data, exclude)
                for p in paths:
                    cat = self.extractor.categorize(p)
                    if cat:
                        result[cat].add(p)
        except Exception as e:
            self.log(f"Ошибка: {e}")
        
        total = sum(len(v) for v in result.values())
        self.log(f"Найдено: {total}")
        
        return dict(result)


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
    
    def create(self, scene_path, archive_path, categories, organize=True, progress_func=None):
        
        stats = {'added': 0, 'missing': 0, 'errors': []}
        
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
            
            for cat, paths in paths_dict.items():
                for p in paths:
                    if os.path.exists(p):
                        found[cat].add(p)
                    else:
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
                for cat, paths in found.items():
                    if categories.get(cat, False):
                        for p in paths:
                            files_list.append((p, cat))
                
                total = len(files_list)
                
                for idx, (fpath, cat) in enumerate(files_list):
                    try:
                        fname = os.path.basename(fpath)
                        
                        if organize:
                            arcname = f"maps/{fname}"
                        else:
                            arcname = fname
                        
                        # Конфликт имён
                        if arcname.lower() in used_names:
                            base, ext = os.path.splitext(arcname)
                            counter = 1
                            while f"{base}_{counter}{ext}".lower() in used_names:
                                counter += 1
                            arcname = f"{base}_{counter}{ext}"
                        
                        used_names.add(arcname.lower())
                        zf.write(fpath, arcname)
                        stats['added'] += 1
                        
                        self.log(f"+ {arcname}")
                        
                    except Exception as e:
                        stats['errors'].append(str(e))
                        self.log(f"! Ошибка: {e}")
                    
                    if progress_func and total > 0:
                        progress_func(40 + int(idx / total * 55))
                
                # Отчёт
                report = self.make_report(scene_path, archive_path, found, missing, categories)
                zf.writestr("_report.txt", report.encode('utf-8'))
                stats['added'] += 1
            
            if progress_func:
                progress_func(100)
            
            size_mb = os.path.getsize(archive_path) / 1024 / 1024
            self.log(f"Готово! Файлов: {stats['added']}, Размер: {size_mb:.1f} MB")
            
            return True, stats['added'], size_mb
            
        except Exception as e:
            self.log(f"ОШИБКА: {e}")
            return False, 0, 0
    
    def make_report(self, scene, archive, found, missing, categories):
        lines = [
            "=" * 50,
            "ОТЧЁТ АРХИВАЦИИ",
            "=" * 50,
            "",
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Сцена: {scene}",
            f"Архив: {archive}",
            "",
            "Автор: @RomanCG",
            "GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2",
            "Behance: https://www.behance.net/Romancg62fe",
            "",
            "-" * 50,
            "ДОБАВЛЕНО:",
        ]
        
        for cat, paths in found.items():
            if categories.get(cat, False) and paths:
                lines.append(f"\n[{cat.upper()}]")
                for p in sorted(paths):
                    lines.append(f"  {os.path.basename(p)}")
        
        if any(missing.values()):
            lines.append("\n" + "-" * 50)
            lines.append("ОТСУТСТВУЕТ:")
            for cat, paths in missing.items():
                if paths:
                    lines.append(f"\n[{cat.upper()}]")
                    for p in sorted(paths):
                        lines.append(f"  {p}")
        
        return '\n'.join(lines)


class App:
    
    VERSION = "2.1"
    GITHUB = "https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2"
    BEHANCE = "https://www.behance.net/Romancg62fe"
    
    def __init__(self, root):
        self.root = root
        self.root.title(f"3Ds Max Scene Archiver in ZIP v{self.VERSION}")
        self.root.geometry("850x700")
        
        self.scene_var = tk.StringVar()
        self.archive_var = tk.StringVar()
        self.organize_var = tk.BooleanVar(value=True)
        
        self.cat_vars = {
            'texture': tk.BooleanVar(value=True),
            'ies': tk.BooleanVar(value=True),
            'proxy': tk.BooleanVar(value=True),
            'cache': tk.BooleanVar(value=True),
            'audio': tk.BooleanVar(value=True),
            'lut': tk.BooleanVar(value=True),
        }
        
        self.archiver = Archiver(self.log)
        self.build_ui()
    
    def build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill='both', expand=True)
        
        # Заголовок
        ttk.Label(main, text=f"3Ds Max Scene Archiver in ZIP v{self.VERSION}",
                 font=('Segoe UI', 14, 'bold')).pack(pady=(0, 10))
        
        # Файлы
        file_frame = ttk.Frame(main)
        file_frame.pack(fill='x', pady=10)
        
        ttk.Label(file_frame, text="Сцена:").grid(row=0, column=0, sticky='w')
        ttk.Entry(file_frame, textvariable=self.scene_var, width=60).grid(row=0, column=1, padx=5)
        ttk.Button(file_frame, text="...", width=3, command=self.browse_scene).grid(row=0, column=2)
        
        ttk.Label(file_frame, text="Архив:").grid(row=1, column=0, sticky='w', pady=(5,0))
        ttk.Entry(file_frame, textvariable=self.archive_var, width=60).grid(row=1, column=1, padx=5, pady=(5,0))
        ttk.Button(file_frame, text="...", width=3, command=self.browse_archive).grid(row=1, column=2, pady=(5,0))
        
        # Категории
        cat_frame = ttk.LabelFrame(main, text="Типы файлов", padding=5)
        cat_frame.pack(fill='x', pady=10)
        
        labels = {'texture': 'Текстуры', 'ies': 'IES', 'proxy': 'Прокси', 
                 'cache': 'Кэш', 'audio': 'Аудио', 'lut': 'LUT'}
        
        for i, (key, label) in enumerate(labels.items()):
            ttk.Checkbutton(cat_frame, text=label, variable=self.cat_vars[key]).grid(
                row=0, column=i, padx=10)
        
        # Настройки
        ttk.Checkbutton(main, text="Ресурсы сохранить в папку maps/ (иначе сохранить в корень архива)", 
                       variable=self.organize_var).pack(anchor='w')
        
        # Кнопки
        btn_frame = ttk.Frame(main)
        btn_frame.pack(pady=10)
        
        self.btn_archive = ttk.Button(btn_frame, text="📦 Создать архив", 
                                     command=self.start_archive)
        self.btn_archive.pack(side='left', padx=5)
        
        ttk.Button(btn_frame, text="📋 Анализ", command=self.start_analyze).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="🗑 Очистить", command=self.clear_log).pack(side='left', padx=5)
        
        # Прогресс
        self.progress = ttk.Progressbar(main, mode='determinate')
        self.progress.pack(fill='x', pady=5)
        
        # Статус
        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(main, textvariable=self.status_var).pack(anchor='w')
        
        # Лог
        self.log_text = scrolledtext.ScrolledText(main, height=15, font=('Consolas', 9))
        self.log_text.pack(fill='both', expand=True, pady=5)
        
        # Футер
        footer = ttk.Frame(main)
        footer.pack(fill='x', pady=(10, 0))
        
        ttk.Separator(footer, orient='horizontal').pack(fill='x')
        
        links = ttk.Frame(footer)
        links.pack(pady=5)
        
        ttk.Label(links, text="@RomanCG", font=('Segoe UI', 9, 'bold')).pack(side='left', padx=10)
        
        gh = tk.Label(links, text="GitHub", fg='#0066cc', cursor='hand2', 
                     font=('Segoe UI', 9, 'underline'))
        gh.pack(side='left', padx=5)
        gh.bind('<Button-1>', lambda e: webbrowser.open(self.GITHUB))
        
        ttk.Label(links, text="•").pack(side='left')
        
        be = tk.Label(links, text="Behance", fg='#0066cc', cursor='hand2',
                     font=('Segoe UI', 9, 'underline'))
        be.pack(side='left', padx=5)
        be.bind('<Button-1>', lambda e: webbrowser.open(self.BEHANCE))
    
    def browse_scene(self):
        f = filedialog.askopenfilename(filetypes=[("3ds Max", "*.max")])
        if f:
            self.scene_var.set(f)
            if not self.archive_var.get():
                self.archive_var.set(os.path.splitext(f)[0] + "_archive.zip")
    
    def browse_archive(self):
        f = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if f:
            self.archive_var.set(f)
    
    def log(self, msg):
        def update():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert('end', f"[{ts}] {msg}\n")
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
        self.root.after(0, lambda: self.btn_archive.configure(state=state))
    
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
            
            ok, count, size = self.archiver.create(
                self.scene_var.get(),
                self.archive_var.get(),
                cats,
                self.organize_var.get(),
                self.set_progress
            )
            
            if ok:
                self.set_status(f"Готово: {count} файлов, {size:.1f} MB")
                messagebox.showinfo("Успех", f"Архив создан!\n\nФайлов: {count}\nРазмер: {size:.1f} MB")
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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
