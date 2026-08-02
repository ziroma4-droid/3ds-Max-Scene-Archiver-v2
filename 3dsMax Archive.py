#!/usr/bin/env python3
"""
Max Scene Packager v3.0
Автор: @RomanCG
GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2
Telegram: https://t.me/Romak04
Email: romancg@yandex.ru
"""

import os
import re
import sys
import ctypes
import glob
import zipfile
import logging
import webbrowser
import subprocess
import tempfile
import time
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, field
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading

try:
    import winreg
except ImportError:
    winreg = None

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


@dataclass(frozen=True)
class MaxInstallation:
    year: int
    install_dir: str
    max_exe: str
    batch_exe: str = ""
    product_version: str = ""

    @property
    def launcher(self):
        return self.batch_exe if self.batch_exe else self.max_exe

    @property
    def display_name(self):
        mode = "Batch" if self.batch_exe else "совместимый режим"
        return f"3ds Max {self.year} · {mode}"

    @property
    def target_versions(self):
        first = self.year - 1
        last = max(2010, self.year - 3)
        if first < 2010:
            return []
        return list(range(first, last - 1, -1))


@dataclass(frozen=True)
class ConversionSettings:
    enabled: bool = False
    installation: MaxInstallation = None
    target_version: int = None


@dataclass
class ConvertedScene:
    source_path: str
    converted_path: str
    target_version: int
    duration_seconds: float
    file_version_data: tuple = ()
    warnings: list = field(default_factory=list)


def windows_file_product_version(filepath):
    """Return (version string, product major) from a Windows executable."""
    if sys.platform != 'win32' or not os.path.isfile(filepath):
        return "", None

    class VSFixedFileInfo(ctypes.Structure):
        _fields_ = [
            ('dwSignature', ctypes.c_uint32),
            ('dwStrucVersion', ctypes.c_uint32),
            ('dwFileVersionMS', ctypes.c_uint32),
            ('dwFileVersionLS', ctypes.c_uint32),
            ('dwProductVersionMS', ctypes.c_uint32),
            ('dwProductVersionLS', ctypes.c_uint32),
            ('dwFileFlagsMask', ctypes.c_uint32),
            ('dwFileFlags', ctypes.c_uint32),
            ('dwFileOS', ctypes.c_uint32),
            ('dwFileType', ctypes.c_uint32),
            ('dwFileSubtype', ctypes.c_uint32),
            ('dwFileDateMS', ctypes.c_uint32),
            ('dwFileDateLS', ctypes.c_uint32),
        ]

    try:
        handle = ctypes.c_uint32(0)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(filepath, ctypes.byref(handle))
        if not size:
            return "", None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(filepath, 0, size, buffer):
            return "", None
        value = ctypes.c_void_p()
        value_size = ctypes.c_uint32(0)
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)
        ):
            return "", None
        info = ctypes.cast(value, ctypes.POINTER(VSFixedFileInfo)).contents
        values = (
            info.dwProductVersionMS >> 16,
            info.dwProductVersionMS & 0xFFFF,
            info.dwProductVersionLS >> 16,
            info.dwProductVersionLS & 0xFFFF,
        )
        return '.'.join(str(value) for value in values), values[0]
    except (AttributeError, OSError, ValueError):
        return "", None


def max_installation_from_path(path):
    path = os.path.abspath(path)
    install_dir = path if os.path.isdir(path) else os.path.dirname(path)
    max_exe = os.path.join(install_dir, '3dsmax.exe')
    if os.path.isfile(path) and os.path.basename(path).lower() == '3dsmax.exe':
        max_exe = path
    if not os.path.isfile(max_exe):
        return None

    product_version, product_major = windows_file_product_version(max_exe)
    year = product_major + 1998 if product_major is not None else None
    if year is None or not 2010 <= year <= 2100:
        match = re.search(r'3ds\s+max\s+(\d{4})', install_dir, re.IGNORECASE)
        year = int(match.group(1)) if match else None
    if year is None:
        return None

    batch_exe = os.path.join(install_dir, '3dsmaxbatch.exe')
    return MaxInstallation(
        year=year,
        install_dir=install_dir,
        max_exe=max_exe,
        batch_exe=batch_exe if os.path.isfile(batch_exe) else "",
        product_version=product_version,
    )


def registry_max_install_locations():
    if winreg is None:
        return []

    locations = []
    uninstall_keys = (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    )
    access_modes = [winreg.KEY_READ]
    if hasattr(winreg, 'KEY_WOW64_64KEY'):
        access_modes.extend((
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        ))

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_name in uninstall_keys:
            for access in access_modes:
                try:
                    with winreg.OpenKey(hive, key_name, 0, access) as key:
                        count = winreg.QueryInfoKey(key)[0]
                        for index in range(count):
                            try:
                                sub_name = winreg.EnumKey(key, index)
                                with winreg.OpenKey(key, sub_name) as sub_key:
                                    display_name = winreg.QueryValueEx(
                                        sub_key, 'DisplayName'
                                    )[0]
                                    if not re.search(
                                        r'Autodesk\s+3ds\s+Max\s+\d{4}',
                                        str(display_name),
                                        re.IGNORECASE,
                                    ):
                                        continue
                                    install_location = winreg.QueryValueEx(
                                        sub_key, 'InstallLocation'
                                    )[0]
                                    if install_location:
                                        locations.append(str(install_location))
                            except (OSError, ValueError):
                                continue
                except OSError:
                    continue
    return locations


def discover_max_installations():
    candidates = set(registry_max_install_locations())

    for name, value in os.environ.items():
        if name.upper().startswith('ADSK_3DSMAX_X64_') and value:
            candidates.add(value)

    for program_files_name in ('ProgramFiles', 'ProgramFiles(x86)'):
        program_files = os.environ.get(program_files_name)
        if not program_files:
            continue
        candidates.update(
            glob.glob(os.path.join(program_files, 'Autodesk', '3ds Max *'))
        )

    installations = {}
    for candidate in candidates:
        installation = max_installation_from_path(candidate)
        if installation is None:
            continue
        key = os.path.normcase(os.path.abspath(installation.max_exe))
        installations[key] = installation

    return sorted(
        installations.values(),
        key=lambda item: (item.year, item.install_dir.casefold()),
        reverse=True,
    )


class MaxVersionConverter:

    TIMEOUT_SECONDS = 30 * 60

    def __init__(self, template_path, log_func=None):
        self.template_path = template_path
        self.log_func = log_func

    def log(self, message):
        if self.log_func:
            self.log_func(message)

    @staticmethod
    def maxscript_verbatim(value):
        if '"' in value:
            raise ValueError('Путь с кавычкой не поддерживается 3ds Max Batch.')
        return value

    @staticmethod
    def read_result(path):
        result = {}
        if not os.path.isfile(path):
            return result
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as result_file:
            for line in result_file:
                key, separator, value = line.rstrip('\r\n').partition('=')
                if separator:
                    result[key] = value
        return result

    @staticmethod
    def read_session_warnings(path):
        if not os.path.isfile(path):
            return []
        warnings = []
        with open(path, 'r', encoding='utf-8', errors='replace') as log_file:
            for line in log_file:
                lowered = line.lower()
                if 'missing dll:' in lowered or 'missing class:' in lowered:
                    message = line.strip()
                    if message and message not in warnings:
                        warnings.append(message)
        return warnings

    @staticmethod
    def terminate_process_tree(process):
        if process.poll() is not None:
            return
        if sys.platform == 'win32':
            subprocess.run(
                ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                check=False,
            )
        else:
            process.kill()

    def make_job_script(self, source_path, output_path, result_path, job_path, target):
        with open(self.template_path, 'r', encoding='utf-8-sig') as template_file:
            script = template_file.read()
        replacements = {
            '__SOURCE_PATH__': self.maxscript_verbatim(source_path),
            '__OUTPUT_PATH__': self.maxscript_verbatim(output_path),
            '__RESULT_PATH__': self.maxscript_verbatim(result_path),
            '__TARGET_VERSION__': str(target),
        }
        for marker, value in replacements.items():
            script = script.replace(marker, value)
        with open(job_path, 'w', encoding='utf-8-sig', newline='\n') as job_file:
            job_file.write(script)

    def convert(self, source_path, output_path, installation, target_version):
        started = time.monotonic()
        work_dir = os.path.dirname(output_path)
        os.makedirs(work_dir, exist_ok=True)
        result_path = os.path.join(work_dir, 'conversion_result.txt')
        job_path = os.path.join(work_dir, 'convert_job.ms')
        session_log = os.path.join(work_dir, '3dsmax_session.log')
        listener_log = os.path.join(work_dir, 'maxscript_listener.log')
        self.make_job_script(
            os.path.abspath(source_path),
            os.path.abspath(output_path),
            os.path.abspath(result_path),
            os.path.abspath(job_path),
            target_version,
        )

        if installation.batch_exe:
            command = [
                installation.batch_exe,
                job_path,
                '-v', '2',
                '-dm', 'on',
                '-log', session_log,
                '-listenerlog', listener_log,
            ]
            if installation.year >= 2022:
                command.extend(('-safescene', 'ON'))
        else:
            command = [
                installation.max_exe,
                '-q',
                '-mi',
                '-silent',
                '-U', 'MAXScript', job_path,
            ]

        self.log(
            f"Запуск 3ds Max {installation.year}: "
            f"сохранение для версии {target_version}"
        )
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            process = subprocess.Popen(
                command,
                cwd=installation.install_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors='replace',
                creationflags=creation_flags,
            )
            try:
                process_output, _unused_stderr = process.communicate(
                    timeout=self.TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as exc:
                self.terminate_process_tree(process)
                process.communicate()
                raise RuntimeError(
                    f"3ds Max не завершил пересохранение за "
                    f"{self.TIMEOUT_SECONDS // 60} минут."
                ) from exc
        except RuntimeError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"3ds Max не завершил пересохранение за "
                f"{self.TIMEOUT_SECONDS // 60} минут."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Не удалось запустить 3ds Max: {exc}") from exc

        result = self.read_result(result_path)
        if result.get('status') != 'success' or not os.path.isfile(output_path):
            message = result.get('message') or f"код завершения {process.returncode}"
            output_tail = (process_output or '').strip().splitlines()[-5:]
            if output_tail:
                message += "; " + ' | '.join(output_tail)
            raise RuntimeError(f"Пересохранение не выполнено: {message}")
        if os.path.getsize(output_path) == 0:
            raise RuntimeError("3ds Max создал пустой файл сцены.")

        version_data = ()
        try:
            file_version = int(result['file_version'])
            saved_by_version = int(result['saved_by_version'])
            version_data = (file_version, saved_by_version)
            expected_file_version = (target_version - 1998) * 1000
            if file_version != expected_file_version:
                raise RuntimeError(
                    f"Проверка версии не пройдена: ожидалось "
                    f"{expected_file_version}, получено {file_version}."
                )
        except KeyError:
            # getMaxFileVersionData is unavailable in 3ds Max 2011/2012.
            version_data = ()

        duration = time.monotonic() - started
        warnings = self.read_session_warnings(session_log)
        for warning in warnings:
            self.log(f"Предупреждение 3ds Max: {warning}")
        self.log(
            f"Пересохранено для 3ds Max {target_version}: "
            f"{os.path.basename(source_path)} ({duration:.1f} сек.)"
        )
        return ConvertedScene(
            source_path=source_path,
            converted_path=output_path,
            target_version=target_version,
            duration_seconds=duration,
            file_version_data=version_data,
            warnings=warnings,
        )


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

    AUTODESK_MAX_ASSET_DIRS = ('maps', 'sceneassets', 'materiallibraries')
    AUTODESK_PROGRAMDATA_PLUGIN_PATTERNS = (
        '3dsmax-civilview-*',
        'AdvancedModeling3dsMax*',
        'Retopology3dsMax*',
        'SubstanceIn3dsMax*',
    )
    
    def __init__(self, log_func=None):
        self.log_func = log_func
        self.extractor = PathExtractor()
        self.local_asset_indexes = {}
        self.system_asset_index = None
        self.system_asset_roots = None
    
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
    def add_unique_path(paths, path):
        if not path:
            return

        normalized = os.path.normpath(path)
        key = os.path.normcase(os.path.abspath(normalized))
        if key not in paths:
            paths[key] = normalized

    def default_system_asset_roots(self):
        roots = {}

        for env_name in ('ProgramFiles', 'ProgramFiles(x86)'):
            program_files = os.environ.get(env_name)
            if not program_files:
                continue

            self.add_unique_path(
                roots,
                os.path.join(
                    program_files,
                    'Common Files',
                    'Autodesk Shared',
                    'Materials',
                )
            )

            autodesk_dir = os.path.join(program_files, 'Autodesk')
            for max_dir in glob.glob(os.path.join(autodesk_dir, '3ds Max*')):
                if not os.path.isdir(max_dir):
                    continue

                dirname = os.path.basename(max_dir).lower()
                if 'sdk' in dirname:
                    continue

                for asset_dir in self.AUTODESK_MAX_ASSET_DIRS:
                    self.add_unique_path(roots, os.path.join(max_dir, asset_dir))

        program_data = os.environ.get('ProgramData')
        if program_data:
            plugins_dir = os.path.join(
                program_data, 'Autodesk', 'ApplicationPlugins'
            )
            for pattern in self.AUTODESK_PROGRAMDATA_PLUGIN_PATTERNS:
                for plugin_dir in glob.glob(os.path.join(plugins_dir, pattern)):
                    self.add_unique_path(roots, os.path.join(plugin_dir, 'Contents'))

        return sorted(
            (path for path in roots.values() if os.path.isdir(path)),
            key=str.casefold,
        )

    def build_system_asset_index(self):
        if self.system_asset_index is not None:
            return self.system_asset_index

        roots = self.default_system_asset_roots()
        self.system_asset_roots = roots
        index = defaultdict(list)

        for root_dir in roots:
            try:
                for root, dirs, filenames in os.walk(root_dir):
                    dirs.sort(key=str.casefold)
                    for filename in sorted(filenames, key=str.casefold):
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in self.extractor.ALL_EXT:
                            index[filename.lower()].append(os.path.join(root, filename))
            except OSError as e:
                self.log(
                    f"Не удалось просканировать библиотеку Autodesk: {root_dir}: {e}"
                )

        if roots:
            self.log(f"Библиотеки Autodesk для поиска: {len(roots)}")

        self.system_asset_index = index
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

    def find_in_system_libraries(self, path):
        filename = path.replace('/', '\\').rsplit('\\', 1)[-1]
        if not filename:
            return None

        candidates = self.build_system_asset_index().get(filename.lower(), [])
        if not candidates:
            return None

        requested_parts = self.path_parts(path)

        def score(candidate):
            candidate_parts = self.path_parts(candidate)
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

        system_path = self.find_in_system_libraries(path)
        if system_path:
            self.log(
                "Найдено в библиотеке Autodesk: "
                f"{os.path.basename(path)} -> {system_path}"
            )
            return system_path

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
    
    def create(
        self,
        scene_path,
        archive_path,
        categories,
        organize=True,
        progress_func=None,
        archive_scene_path=None,
        archive_scene_name=None,
        file_overrides=None,
        conversion_info=None,
    ):
        
        stats = {
            'added': 0,
            'resources': 0,
            'missing': 0,
            'errors': [],
            'conversion': conversion_info,
            'conversion_warnings': len(
                conversion_info.get('warnings', []) if conversion_info else []
            ),
        }
        file_overrides = file_overrides or {}
        
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
                scene_source = archive_scene_path or scene_path
                scene_name = archive_scene_name or os.path.basename(scene_path)
                zf.write(scene_source, scene_name)
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
                        
                        override_key = os.path.normcase(os.path.abspath(fpath))
                        archive_source = file_overrides.get(override_key, fpath)
                        zf.write(archive_source, arcname)
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
                    stats['errors'], stats['resources'], conversion_info,
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
        self,
        scene,
        archive,
        archived,
        missing,
        categories,
        errors,
        resource_count,
        conversion_info=None,
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
        ]

        if conversion_info:
            lines.extend([
                "-" * 50,
                "ПЕРЕСОХРАНЕНИЕ:",
                f"Запущен 3ds Max: {conversion_info['runtime_version']}",
                f"Целевая версия: {conversion_info['target_version']}",
                f"Исполняемый файл: {conversion_info['runtime_path']}",
                f"Сцена в архиве: {conversion_info['scene_name']}",
                f"XRef пересохранено: {conversion_info['xref_count']}",
                f"Время пересохранения: {conversion_info['duration_seconds']:.1f} сек.",
                "",
            ])
            if conversion_info.get('warnings'):
                lines.append("ПРЕДУПРЕЖДЕНИЯ 3DS MAX:")
                for warning in conversion_info['warnings']:
                    lines.append(f"  {warning}")
                lines.append("")

        lines.extend([
            "Автор: @RomanCG",
            "GitHub: https://github.com/ziroma4-droid/3ds-Max-Scene-Archiver-v2",
            "Telegram: https://t.me/Romak04",
            "Email: romancg@yandex.ru",
            "",
            "-" * 50,
            "ДОБАВЛЕНО:",
        ])
        
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


@dataclass
class BatchItem:
    scene_path: str
    archive_name: str
    status: str = "Ожидает"
    progress: int = 0
    result: dict = field(default_factory=dict)
    iid: str = ""


class App:

    APP_NAME = "Max Scene Packager"
    VERSION = "3.0"
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
        self.root.geometry("1000x840")
        self.root.minsize(860, 700)
        self.root.configure(bg=self.COLORS['background'])
        self.set_window_icon()

        available_fonts = set(tkfont.families(self.root))
        self.font_family = 'Inter' if 'Inter' in available_fonts else 'Segoe UI'
        self.configure_styles()
        
        self.scene_var = tk.StringVar()
        self.archive_var = tk.StringVar()
        self.scene_var.trace_add('write', self.on_scene_path_changed)
        self.organize_var = tk.BooleanVar(value=True)
        self.convert_version_var = tk.BooleanVar(value=False)
        self.max_install_var = tk.StringVar()
        self.target_version_var = tk.StringVar()
        self.conversion_message_var = tk.StringVar()
        self.max_installations = []
        self.max_install_lookup = {}
        self.max_scan_complete = False
        self.batch_output_mode = tk.StringVar(value='alongside')
        self.batch_output_dir = tk.StringVar()
        self.batch_items = []
        self.batch_running = False
        self.batch_stop_requested = False
        self.batch_name_editor = None
        
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
        self.converter = MaxVersionConverter(
            self.resource_path(os.path.join('assets', 'convert_max_version.ms')),
            self.log,
        )
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
        style.configure('PanelWarning.TLabel',
                        background=colors['panel'],
                        foreground=colors['warning'],
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

        style.configure('TCombobox',
                        fieldbackground=colors['surface'],
                        background=colors['surface'],
                        foreground=colors['text'],
                        arrowcolor=colors['secondary'],
                        bordercolor=colors['border'],
                        lightcolor=colors['border'],
                        darkcolor=colors['border'],
                        padding=(8, 6))
        style.map('TCombobox',
                  fieldbackground=[('readonly', colors['surface']),
                                   ('disabled', colors['panel'])],
                  foreground=[('readonly', colors['text']),
                              ('disabled', colors['disabled'])],
                  bordercolor=[('focus', colors['accent'])])

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

        style.configure('Panel.TRadiobutton',
                        background=colors['panel'],
                        foreground=colors['text'],
                        font=(self.font_family, 9),
                        padding=(0, 2),
                        indicatorbackground=colors['surface'],
                        indicatorforeground='#FFFFFF',
                        bordercolor=colors['border'])
        style.map('Panel.TRadiobutton',
                  background=[('active', colors['panel'])],
                  foreground=[('disabled', colors['disabled'])],
                  indicatorbackground=[('selected', colors['accent']),
                                       ('active', '#30353A')],
                  indicatorforeground=[('selected', '#FFFFFF')])

        style.configure('Workspace.TNotebook',
                        background=colors['background'],
                        borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure('Workspace.TNotebook.Tab',
                        background=colors['surface'],
                        foreground=colors['secondary'],
                        padding=(16, 8),
                        borderwidth=0,
                        font=(self.font_family, 9))
        style.map('Workspace.TNotebook.Tab',
                  background=[('selected', colors['panel']), ('active', '#30343A')],
                  foreground=[('selected', colors['text']), ('active', colors['text'])])

        style.configure('Batch.Treeview',
                        background=colors['background'],
                        fieldbackground=colors['background'],
                        foreground=colors['secondary'],
                        bordercolor=colors['border'],
                        rowheight=25,
                        relief='flat',
                        font=(self.font_family, 9))
        style.map('Batch.Treeview',
                  background=[('selected', '#28446F')],
                  foreground=[('selected', '#FFFFFF')])
        style.configure('Batch.Treeview.Heading',
                        background=colors['surface'],
                        foreground=colors['text'],
                        bordercolor=colors['border'],
                        relief='flat',
                        padding=(8, 6),
                        font=(self.font_family, 9))
        style.map('Batch.Treeview.Heading',
                  background=[('active', '#30343A')])

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

        self.file_notebook = ttk.Notebook(workspace, style='Workspace.TNotebook')
        self.file_notebook.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.single_tab = ttk.Frame(self.file_notebook, style='Panel.TFrame')
        self.batch_tab = ttk.Frame(self.file_notebook, style='Panel.TFrame')
        self.file_notebook.add(self.single_tab, text='Одна сцена')
        self.file_notebook.add(self.batch_tab, text='Пакет')

        file_card = ttk.Frame(self.single_tab, padding=(20, 16), style='Panel.TFrame')
        file_card.pack(fill='both', expand=True)
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

        self.build_batch_panel(self.batch_tab)

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

        self.category_checkbuttons = []
        for i, (key, label) in enumerate(labels.items()):
            checkbutton = ttk.Checkbutton(
                options_card,
                text=label,
                variable=self.cat_vars[key],
                style='Panel.TCheckbutton',
            )
            checkbutton.grid(
                row=2 + i // 2,
                column=i % 2,
                sticky='w',
                padx=(0, 12),
                pady=1,
            )
            self.category_checkbuttons.append(checkbutton)

        ttk.Separator(options_card).grid(
            row=6, column=0, columnspan=2, sticky='ew', pady=(8, 8)
        )
        self.organize_checkbutton = ttk.Checkbutton(
            options_card,
            text="Структура maps/ и xrefs/",
            variable=self.organize_var,
            style='Panel.TCheckbutton',
        )
        self.organize_checkbutton.grid(row=7, column=0, columnspan=2, sticky='w')

        ttk.Separator(options_card).grid(
            row=8, column=0, columnspan=2, sticky='ew', pady=(8, 8)
        )
        self.convert_checkbutton = ttk.Checkbutton(
            options_card,
            text="Пересохранить для старой версии",
            variable=self.convert_version_var,
            command=self.on_conversion_toggled,
            style='Panel.TCheckbutton',
        )
        self.convert_checkbutton.grid(row=9, column=0, columnspan=2, sticky='w')

        self.conversion_frame = ttk.Frame(options_card, style='Panel.TFrame')
        self.conversion_frame.grid(
            row=10,
            column=0,
            columnspan=2,
            sticky='ew',
            pady=(8, 0),
        )
        self.conversion_frame.columnconfigure(1, weight=1)
        ttk.Label(
            self.conversion_frame,
            text="Запускать",
            style='PanelMuted.TLabel',
        ).grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.max_install_combo = ttk.Combobox(
            self.conversion_frame,
            textvariable=self.max_install_var,
            state='readonly',
            width=23,
        )
        self.max_install_combo.grid(row=0, column=1, sticky='ew')
        self.max_install_combo.bind('<<ComboboxSelected>>', self.on_max_install_selected)
        self.btn_browse_max = ttk.Button(
            self.conversion_frame,
            text="EXE",
            command=self.browse_max_executable,
            style='Quiet.TButton',
        )
        self.btn_browse_max.grid(row=0, column=2, padx=(4, 0))

        ttk.Label(
            self.conversion_frame,
            text="Формат",
            style='PanelMuted.TLabel',
        ).grid(row=1, column=0, sticky='w', padx=(0, 8), pady=(6, 0))
        self.target_version_combo = ttk.Combobox(
            self.conversion_frame,
            textvariable=self.target_version_var,
            state='readonly',
            width=23,
        )
        self.target_version_combo.grid(
            row=1,
            column=1,
            columnspan=2,
            sticky='ew',
            pady=(6, 0),
        )
        ttk.Label(
            self.conversion_frame,
            textvariable=self.conversion_message_var,
            style='PanelMuted.TLabel',
            wraplength=300,
        ).grid(row=2, column=0, columnspan=3, sticky='w', pady=(7, 0))
        ttk.Label(
            self.conversion_frame,
            text=(
                "⚠ Будет запущен 3ds Max. Использование CPU и памяти, "
                "а также время упаковки увеличатся."
            ),
            style='PanelWarning.TLabel',
            wraplength=300,
        ).grid(row=3, column=0, columnspan=3, sticky='w', pady=(5, 0))
        self.conversion_frame.grid_remove()

        controls = ttk.Frame(main, style='App.TFrame')
        controls.pack(fill='x', pady=(0, 12))
        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(controls, textvariable=self.status_var, style='Status.TLabel').pack(
            side='left'
        )

        self.single_actions = ttk.Frame(controls, style='App.TFrame')
        self.single_actions.pack(side='right')
        self.btn_archive = ttk.Button(
            self.single_actions,
            text="Создать архив",
            command=self.start_archive,
            style='Primary.TButton',
        )
        self.btn_archive.pack(side='right')
        self.btn_analyze = ttk.Button(
            self.single_actions,
            text="Анализировать",
            command=self.start_analyze,
            style='Secondary.TButton',
        )
        self.btn_analyze.pack(side='right', padx=(0, 8))

        self.batch_actions = ttk.Frame(controls, style='App.TFrame')
        self.btn_batch_start = ttk.Button(
            self.batch_actions,
            text="Архивировать пакет",
            command=self.start_batch_archive,
            style='Primary.TButton',
        )
        self.btn_batch_start.pack(side='right')
        self.btn_batch_stop = ttk.Button(
            self.batch_actions,
            text="Остановить после текущего",
            command=self.request_batch_stop,
            style='Secondary.TButton',
            state='disabled',
        )
        self.btn_batch_stop.pack(side='right', padx=(0, 8))
        self.file_notebook.bind('<<NotebookTabChanged>>', self.on_mode_changed)

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

        self.on_batch_output_mode_changed(reset_statuses=False)
        self.update_batch_start_label()

    def on_conversion_toggled(self):
        if self.convert_version_var.get():
            self.conversion_frame.grid()
            if not self.max_scan_complete:
                self.refresh_max_installations()
            else:
                self.update_conversion_controls()
        else:
            self.conversion_frame.grid_remove()

    def refresh_max_installations(self):
        self.max_installations = discover_max_installations()
        self.max_scan_complete = True
        self.rebuild_max_install_lookup()
        self.update_conversion_controls()

    def rebuild_max_install_lookup(self, preferred=None):
        self.max_install_lookup = {}
        values = []
        for installation in self.max_installations:
            label = f"3ds Max {installation.year}"
            if any(existing.startswith(label) for existing in values):
                label = f"{label} · {installation.install_dir}"
            values.append(label)
            self.max_install_lookup[label] = installation
        self.max_install_combo.configure(values=values)

        selected_label = None
        if preferred is not None:
            for label, installation in self.max_install_lookup.items():
                if os.path.normcase(installation.max_exe) == os.path.normcase(
                    preferred.max_exe
                ):
                    selected_label = label
                    break
        if selected_label is None and values:
            selected_label = values[0]
        self.max_install_var.set(selected_label or "")

    def update_conversion_controls(self):
        installation = self.max_install_lookup.get(self.max_install_var.get())
        if installation is None:
            self.max_install_combo.configure(state='disabled')
            self.target_version_combo.configure(state='disabled', values=())
            self.target_version_var.set("")
            self.conversion_message_var.set(
                "3ds Max не найден. Укажите установленный 3dsmax.exe."
            )
            return

        self.max_install_combo.configure(state='readonly')
        targets = [f"3ds Max {year}" for year in installation.target_versions]
        self.target_version_combo.configure(
            state='readonly' if targets else 'disabled',
            values=targets,
        )
        if self.target_version_var.get() not in targets:
            self.target_version_var.set(targets[0] if targets else "")

        launcher_name = (
            '3dsmaxbatch.exe' if installation.batch_exe else '3dsmax.exe'
        )
        if targets:
            self.conversion_message_var.set(
                f"Найден 3ds Max {installation.year} ({launcher_name}). "
                "Основная сцена и .max-XRef будут пересохранены."
            )
        else:
            self.conversion_message_var.set(
                f"3ds Max {installation.year} не поддерживает сохранение назад."
            )

    def on_max_install_selected(self, _event=None):
        self.update_conversion_controls()

    def browse_max_executable(self):
        filename = filedialog.askopenfilename(
            title="Укажите 3dsmax.exe",
            filetypes=[("3ds Max", "3dsmax.exe"), ("EXE", "*.exe")],
        )
        if not filename:
            return
        installation = max_installation_from_path(filename)
        if installation is None:
            messagebox.showerror(
                "3ds Max не найден",
                "Не удалось определить версию выбранного 3dsmax.exe.",
            )
            return
        known = {
            os.path.normcase(os.path.abspath(item.max_exe))
            for item in self.max_installations
        }
        if os.path.normcase(os.path.abspath(installation.max_exe)) not in known:
            self.max_installations.append(installation)
            self.max_installations.sort(key=lambda item: item.year, reverse=True)
        self.rebuild_max_install_lookup(preferred=installation)
        self.update_conversion_controls()

    def get_conversion_settings(self):
        if not self.convert_version_var.get():
            return ConversionSettings(enabled=False)

        installation = self.max_install_lookup.get(self.max_install_var.get())
        match = re.search(r'(\d{4})', self.target_version_var.get())
        target_version = int(match.group(1)) if match else None
        if installation is None:
            messagebox.showerror(
                "Пересохранение недоступно",
                "Укажите установленный 3dsmax.exe.",
            )
            return None
        if target_version not in installation.target_versions:
            messagebox.showerror(
                "Некорректная версия",
                "Выберите одну из доступных предыдущих версий 3ds Max.",
            )
            return None
        return ConversionSettings(
            enabled=True,
            installation=installation,
            target_version=target_version,
        )

    def build_batch_panel(self, parent):
        colors = self.COLORS
        panel = ttk.Frame(parent, padding=(16, 12), style='Panel.TFrame')
        panel.pack(fill='both', expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)

        header = ttk.Frame(panel, style='Panel.TFrame')
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text="Очередь сцен", style='Section.TLabel').pack(side='left')
        self.btn_batch_add = ttk.Button(
            header,
            text="Добавить сцены",
            command=self.add_batch_scenes,
            style='Secondary.TButton',
        )
        self.btn_batch_add.pack(side='right')

        tools_row = ttk.Frame(panel, style='Panel.TFrame')
        tools_row.grid(row=1, column=0, sticky='ew', pady=(6, 8))
        ttk.Label(
            tools_row,
            text="Имя ZIP редактируется двойным щелчком",
            style='PanelMuted.TLabel',
        ).pack(side='left')
        self.btn_batch_clear = ttk.Button(
            tools_row,
            text="Очистить",
            command=self.clear_batch_items,
            style='Quiet.TButton',
        )
        self.btn_batch_clear.pack(side='right')
        self.btn_batch_remove = ttk.Button(
            tools_row,
            text="Удалить",
            command=self.remove_batch_items,
            style='Quiet.TButton',
        )
        self.btn_batch_remove.pack(side='right', padx=(0, 4))

        tree_frame = ttk.Frame(panel, style='Panel.TFrame')
        tree_frame.grid(row=2, column=0, sticky='nsew')
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.batch_tree = ttk.Treeview(
            tree_frame,
            columns=('scene', 'archive', 'status'),
            show='headings',
            height=4,
            selectmode='extended',
            style='Batch.Treeview',
        )
        self.batch_tree.heading('scene', text='Сцена')
        self.batch_tree.heading('archive', text='Имя архива')
        self.batch_tree.heading('status', text='Статус')
        self.batch_tree.column('scene', width=210, minwidth=120, stretch=True)
        self.batch_tree.column('archive', width=165, minwidth=120, stretch=True)
        self.batch_tree.column('status', width=105, minwidth=90, stretch=False)
        self.batch_tree.grid(row=0, column=0, sticky='nsew')
        tree_scroll = ttk.Scrollbar(
            tree_frame,
            orient='vertical',
            command=self.batch_tree.yview,
        )
        tree_scroll.grid(row=0, column=1, sticky='ns')
        self.batch_tree.configure(yscrollcommand=tree_scroll.set)
        self.batch_tree.bind('<Double-1>', self.begin_batch_name_edit)
        self.batch_tree.tag_configure('active', foreground=colors['accent'])
        self.batch_tree.tag_configure('success', foreground=colors['success'])
        self.batch_tree.tag_configure('warning', foreground=colors['warning'])
        self.batch_tree.tag_configure('error', foreground=colors['error'])
        self.batch_tree.tag_configure('muted', foreground=colors['disabled'])

        output = ttk.Frame(panel, style='Panel.TFrame')
        output.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        output.columnconfigure(1, weight=1)
        self.batch_output_radios = []
        alongside_radio = ttk.Radiobutton(
            output,
            text="Сохранять рядом со сценой",
            value='alongside',
            variable=self.batch_output_mode,
            command=self.on_batch_output_mode_changed,
            style='Panel.TRadiobutton',
        )
        alongside_radio.grid(row=0, column=0, columnspan=3, sticky='w')
        directory_radio = ttk.Radiobutton(
            output,
            text="Все архивы в папку",
            value='directory',
            variable=self.batch_output_mode,
            command=self.on_batch_output_mode_changed,
            style='Panel.TRadiobutton',
        )
        directory_radio.grid(row=1, column=0, sticky='w', pady=(4, 0), padx=(0, 8))
        self.batch_output_radios.extend((alongside_radio, directory_radio))
        self.batch_output_entry = ttk.Entry(output, textvariable=self.batch_output_dir)
        self.batch_output_entry.grid(row=1, column=1, sticky='ew', pady=(4, 0))
        self.btn_batch_output = ttk.Button(
            output,
            text="Обзор",
            command=self.browse_batch_output_dir,
            style='Secondary.TButton',
        )
        self.btn_batch_output.grid(row=1, column=2, padx=(8, 0), pady=(4, 0))

    def on_mode_changed(self, _event=None):
        if not hasattr(self, 'batch_actions'):
            return
        is_batch = self.file_notebook.index('current') == 1
        if is_batch:
            self.single_actions.pack_forget()
            self.batch_actions.pack(side='right')
        else:
            self.batch_actions.pack_forget()
            self.single_actions.pack(side='right')

    @staticmethod
    def default_batch_archive_name(scene_path):
        scene_name = os.path.basename(scene_path)
        return os.path.splitext(scene_name)[0] + "_archive.zip"

    @staticmethod
    def validate_archive_filename(name):
        normalized = name.strip()
        if not normalized:
            return False, normalized, "Имя архива не может быть пустым."
        if re.search(r'[<>:"/\\|?*\x00-\x1f]', normalized):
            return False, normalized, "В имени архива есть недопустимые символы."
        if normalized.endswith((' ', '.')):
            return False, normalized, "Имя архива не может заканчиваться пробелом или точкой."
        if not normalized.lower().endswith('.zip'):
            normalized += '.zip'
        stem = os.path.splitext(normalized)[0]
        reserved = {
            'CON', 'PRN', 'AUX', 'NUL',
            'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
            'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
        }
        if not stem or stem.upper() in reserved:
            return False, normalized, "Это имя архива недоступно в Windows."
        return True, normalized, ""

    def add_batch_scenes(self):
        files = filedialog.askopenfilenames(filetypes=[("3ds Max", "*.max")])
        if not files:
            return

        known = {
            os.path.normcase(os.path.abspath(item.scene_path))
            for item in self.batch_items
        }
        added = 0
        skipped = 0
        for scene_path in files:
            key = os.path.normcase(os.path.abspath(scene_path))
            if key in known:
                skipped += 1
                continue
            item = BatchItem(
                scene_path=scene_path,
                archive_name=self.default_batch_archive_name(scene_path),
            )
            item.iid = self.batch_tree.insert(
                '',
                'end',
                values=(item.scene_path, item.archive_name, item.status),
            )
            self.batch_items.append(item)
            known.add(key)
            added += 1

        self.update_batch_start_label()
        if added:
            self.log(f"Пакет: добавлено сцен — {added}")
        if skipped:
            self.log(f"Пакет: повторно выбранные сцены пропущены — {skipped}")

    def remove_batch_items(self):
        selected = set(self.batch_tree.selection())
        if not selected:
            return
        self.batch_items = [item for item in self.batch_items if item.iid not in selected]
        for iid in selected:
            if self.batch_tree.exists(iid):
                self.batch_tree.delete(iid)
        self.update_batch_start_label()

    def clear_batch_items(self):
        for iid in self.batch_tree.get_children():
            self.batch_tree.delete(iid)
        self.batch_items.clear()
        self.update_batch_start_label()

    def update_batch_start_label(self):
        if not hasattr(self, 'btn_batch_start'):
            return
        count = len(self.batch_items)
        text = f"Архивировать: {count}" if count else "Архивировать пакет"
        self.btn_batch_start.configure(text=text)

    def browse_batch_output_dir(self):
        directory = filedialog.askdirectory()
        if directory:
            self.batch_output_dir.set(directory)
            self.batch_output_mode.set('directory')
            self.on_batch_output_mode_changed()

    def on_batch_output_mode_changed(self, reset_statuses=True):
        if not hasattr(self, 'batch_output_entry'):
            return
        directory_mode = self.batch_output_mode.get() == 'directory'
        state = 'normal' if directory_mode and not self.batch_running else 'disabled'
        self.batch_output_entry.configure(state=state)
        self.btn_batch_output.configure(state=state)
        if reset_statuses and not self.batch_running:
            for item in self.batch_items:
                if item.status != "Ожидает":
                    self.render_batch_item(item, "Ожидает")

    def find_batch_item(self, iid):
        return next((item for item in self.batch_items if item.iid == iid), None)

    def begin_batch_name_edit(self, event):
        if self.batch_running or self.batch_tree.identify_region(event.x, event.y) != 'cell':
            return
        iid = self.batch_tree.identify_row(event.y)
        column = self.batch_tree.identify_column(event.x)
        if not iid or column != '#2':
            return
        item = self.find_batch_item(iid)
        bounds = self.batch_tree.bbox(iid, column)
        if item is None or not bounds:
            return

        if self.batch_name_editor is not None:
            self.batch_name_editor.destroy()
        x, y, width, height = bounds
        editor = ttk.Entry(self.batch_tree)
        editor.insert(0, item.archive_name)
        editor.select_range(0, 'end')
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        self.batch_name_editor = editor

        def finish(commit=True):
            if self.batch_name_editor is not editor:
                return
            value = editor.get()
            self.batch_name_editor = None
            editor.destroy()
            if not commit:
                return
            valid, normalized, error = self.validate_archive_filename(value)
            if not valid:
                messagebox.showerror("Некорректное имя", error)
                return
            item.archive_name = normalized
            item.result.clear()
            self.render_batch_item(item, "Ожидает")

        editor.bind('<Return>', lambda _event: finish(True))
        editor.bind('<Escape>', lambda _event: finish(False))
        editor.bind('<FocusOut>', lambda _event: finish(True))

    def batch_status_tag(self, status):
        lowered = status.lower()
        if lowered.startswith(('архивируется', 'пересохранение')):
            return 'active'
        if lowered.startswith('готов с'):
            return 'warning'
        if lowered.startswith('готов'):
            return 'success'
        if lowered.startswith('ошибка'):
            return 'error'
        if lowered.startswith('пропущен'):
            return 'muted'
        return ''

    def render_batch_item(self, item, status=None):
        if status is not None:
            item.status = status
        if not self.batch_tree.exists(item.iid):
            return
        tag = self.batch_status_tag(item.status)
        self.batch_tree.item(
            item.iid,
            values=(item.scene_path, item.archive_name, item.status),
            tags=(tag,) if tag else (),
        )

    def set_batch_item_status(self, item, status, progress=None):
        item.status = status
        if progress is not None:
            item.progress = progress
        self.root.after(0, lambda: self.render_batch_item(item))

    def set_batch_queue_enabled(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for widget in (
            self.btn_batch_add,
            self.btn_batch_remove,
            self.btn_batch_clear,
            *self.batch_output_radios,
        ):
            widget.configure(state=state)
        self.on_batch_output_mode_changed(reset_statuses=False)

    def convert_scene_for_archive(
        self,
        scene_path,
        categories,
        settings,
        temp_root,
        log_func,
        progress_func,
    ):
        target = settings.target_version
        installation = settings.installation
        xrefs = []
        if categories.get('xref', False):
            log_func("Поиск XRef для пересохранения...")
            parsed_paths = MaxParser(log_func).parse(scene_path)
            xrefs = sorted(
                (
                    path for path in parsed_paths.get('xref', set())
                    if os.path.isfile(path)
                    and os.path.splitext(path)[1].lower() == '.max'
                ),
                key=str.casefold,
            )

        jobs = [(scene_path, None)] + [(xref, index) for index, xref in enumerate(xrefs)]
        converter = MaxVersionConverter(
            self.resource_path(os.path.join('assets', 'convert_max_version.ms')),
            log_func,
        )
        converted = []
        file_overrides = {}
        main_name = (
            os.path.splitext(os.path.basename(scene_path))[0]
            + f"_Max{target}.max"
        )

        for position, (source_path, xref_index) in enumerate(jobs, 1):
            if xref_index is None:
                output_dir = os.path.join(temp_root, 'main')
                output_name = main_name
            else:
                output_dir = os.path.join(temp_root, f'xref_{xref_index:04d}')
                output_name = os.path.basename(source_path)
            output_path = os.path.join(output_dir, output_name)
            progress_func(int((position - 1) / len(jobs) * 35))
            result = converter.convert(
                source_path,
                output_path,
                installation,
                target,
            )
            converted.append(result)
            if xref_index is not None:
                override_key = os.path.normcase(os.path.abspath(source_path))
                file_overrides[override_key] = output_path
            progress_func(int(position / len(jobs) * 35))

        conversion_info = {
            'runtime_version': installation.year,
            'target_version': target,
            'runtime_path': installation.launcher,
            'scene_name': main_name,
            'xref_count': len(xrefs),
            'duration_seconds': sum(item.duration_seconds for item in converted),
            'warnings': list(dict.fromkeys(
                warning
                for item in converted
                for warning in item.warnings
            )),
        }
        return {
            'archive_scene_path': converted[0].converted_path,
            'archive_scene_name': main_name,
            'file_overrides': file_overrides,
            'conversion_info': conversion_info,
        }

    def run_archive_job(
        self,
        scene_path,
        archive_path,
        categories,
        organize,
        conversion_settings,
        progress_func,
        log_func,
    ):
        stats = {'added': 0, 'resources': 0, 'missing': 0, 'errors': []}
        try:
            with tempfile.TemporaryDirectory(prefix='max_scene_packager_') as temp_root:
                conversion_context = {}
                archive_progress_start = 0
                if conversion_settings.enabled:
                    conversion_context = self.convert_scene_for_archive(
                        scene_path,
                        categories,
                        conversion_settings,
                        temp_root,
                        log_func,
                        progress_func,
                    )
                    archive_progress_start = 35

                def archive_progress(value):
                    mapped = archive_progress_start + (
                        (100 - archive_progress_start) * float(value) / 100.0
                    )
                    progress_func(mapped)

                archiver = Archiver(log_func)
                return archiver.create(
                    scene_path,
                    archive_path,
                    categories,
                    organize,
                    archive_progress,
                    **conversion_context,
                )
        except Exception as exc:
            log_func(f"ОШИБКА пересохранения: {exc}")
            stats['errors'].append({'path': scene_path, 'message': str(exc)})
            return False, 0, 0, stats

    def prepare_batch_jobs(self):
        if not self.batch_items:
            messagebox.showerror("Ошибка", "Добавьте хотя бы одну сцену.")
            return None

        output_mode = self.batch_output_mode.get()
        output_dir = self.batch_output_dir.get().strip()
        errors = []
        jobs = []
        destinations = defaultdict(list)

        if output_mode == 'directory':
            if not output_dir:
                messagebox.showerror("Ошибка", "Укажите папку для архивов.")
                return None
            output_dir = os.path.abspath(output_dir)

        for item in self.batch_items:
            self.render_batch_item(item, "Ожидает")
            if not os.path.isfile(item.scene_path):
                errors.append(f"Файл сцены не найден: {item.scene_path}")
                self.render_batch_item(item, "Ошибка: сцена не найдена")
                continue
            if os.path.splitext(item.scene_path)[1].lower() != '.max':
                errors.append(f"Неподдерживаемый файл: {item.scene_path}")
                self.render_batch_item(item, "Ошибка: не .max")
                continue

            valid, normalized, error = self.validate_archive_filename(item.archive_name)
            if not valid:
                errors.append(f"{os.path.basename(item.scene_path)}: {error}")
                self.render_batch_item(item, "Ошибка: имя ZIP")
                continue
            item.archive_name = normalized
            target_dir = (
                output_dir
                if output_mode == 'directory'
                else os.path.dirname(item.scene_path)
            )
            archive_path = os.path.abspath(os.path.join(target_dir, item.archive_name))
            key = os.path.normcase(archive_path)
            destinations[key].append(item)
            jobs.append((item, archive_path))

        duplicate_keys = {key for key, items in destinations.items() if len(items) > 1}
        if duplicate_keys:
            for key in duplicate_keys:
                names = ', '.join(os.path.basename(item.scene_path) for item in destinations[key])
                errors.append(f"Одинаковый путь архива: {key} ({names})")
                for item in destinations[key]:
                    self.render_batch_item(item, "Ошибка: совпадает имя")

        if errors:
            preview = '\n'.join(errors[:8])
            if len(errors) > 8:
                preview += f"\n…и ещё {len(errors) - 8}"
            messagebox.showerror("Пакет не готов", preview)
            return None

        if output_mode == 'directory':
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Ошибка папки", str(exc))
                return None

        for item, archive_path in jobs:
            if os.path.isdir(archive_path):
                self.render_batch_item(item, "Ошибка: путь занят папкой")
                messagebox.showerror(
                    "Пакет не готов",
                    f"Путь архива занят папкой:\n{archive_path}",
                )
                return None
        return jobs

    def start_batch_archive(self):
        if self.batch_running:
            return
        conversion_settings = self.get_conversion_settings()
        if conversion_settings is None:
            return
        jobs = self.prepare_batch_jobs()
        if jobs is None:
            return

        existing = [(item, path) for item, path in jobs if os.path.isfile(path)]
        skipped_existing = 0
        if existing:
            choice = messagebox.askyesnocancel(
                "Архивы уже существуют",
                f"Найдено существующих архивов: {len(existing)}.\n\n"
                "Да — заменить их\nНет — пропустить\nОтмена — вернуться к списку",
            )
            if choice is None:
                return
            if not choice:
                existing_paths = {os.path.normcase(path) for _item, path in existing}
                filtered = []
                for item, path in jobs:
                    if os.path.normcase(path) in existing_paths:
                        self.render_batch_item(item, "Пропущен: архив существует")
                        skipped_existing += 1
                    else:
                        filtered.append((item, path))
                jobs = filtered

        if not jobs:
            self.set_status("Пакет: все архивы пропущены")
            messagebox.showinfo("Пакет завершён", "Все архивы уже существуют и были пропущены.")
            return

        categories = {key: var.get() for key, var in self.cat_vars.items()}
        organize = self.organize_var.get()
        self.batch_running = True
        self.batch_stop_requested = False
        self.set_batch_queue_enabled(False)
        self.set_enabled(False)
        self.btn_batch_stop.configure(state='normal', text="Остановить после текущего")
        self.file_notebook.tab(self.single_tab, state='disabled')
        self.set_progress(0)
        self.set_status(f"Пакет: 0 из {len(jobs)}")
        self.log(f"Пакетная обработка: сцен — {len(jobs)}")
        threading.Thread(
            target=self.do_batch_archive,
            args=(
                jobs,
                categories,
                organize,
                skipped_existing,
                conversion_settings,
            ),
            daemon=True,
        ).start()

    def request_batch_stop(self):
        if not self.batch_running or self.batch_stop_requested:
            return
        self.batch_stop_requested = True
        self.btn_batch_stop.configure(state='disabled', text="Остановка после текущего…")
        self.set_status("Пакет: остановка после текущего архива")
        self.log("Пакет: запрошена остановка после текущего архива")

    def do_batch_archive(
        self,
        jobs,
        categories,
        organize,
        skipped_existing,
        conversion_settings=None,
    ):
        conversion_settings = conversion_settings or ConversionSettings(False)
        total = len(jobs)
        summary = {
            'ready': 0,
            'warnings': 0,
            'errors': 0,
            'skipped': skipped_existing,
        }
        processed = 0

        for position, (item, archive_path) in enumerate(jobs, 1):
            if self.batch_stop_requested:
                break

            scene_name = os.path.basename(item.scene_path)
            prefix = f"[{position}/{total}] {scene_name}"
            self.set_batch_item_status(item, "Архивируется · 0%", 0)
            last_progress = {'value': -1}

            def update_progress(value, current=item, number=position, name=scene_name):
                value = max(0, min(100, int(value)))
                if value == last_progress['value']:
                    return
                last_progress['value'] = value
                overall = ((number - 1) + value / 100.0) / total * 100
                self.set_progress(overall)
                self.set_status(
                    f"Пакет: готово {number - 1} из {total} · {name} · {value}%"
                )
                if conversion_settings.enabled and value < 35:
                    row_status = f"Пересохранение · {value}%"
                else:
                    row_status = f"Архивируется · {value}%"
                self.set_batch_item_status(current, row_status, value)

            job_log = lambda msg, current_prefix=prefix: self.log(
                msg, current_prefix
            )
            ok, count, size, stats = self.run_archive_job(
                item.scene_path,
                archive_path,
                categories,
                organize,
                conversion_settings,
                update_progress,
                job_log,
            )
            processed += 1
            item.result = {
                'ok': ok,
                'archive_path': archive_path,
                'count': count,
                'size': size,
                'stats': stats,
            }

            if not ok:
                summary['errors'] += 1
                self.set_batch_item_status(item, "Ошибка", 100)
            elif (
                stats['missing']
                or stats['errors']
                or stats.get('conversion_warnings')
            ):
                summary['warnings'] += 1
                self.set_batch_item_status(item, "Готов с предупреждениями", 100)
            else:
                summary['ready'] += 1
                self.set_batch_item_status(item, "Готов", 100)

            self.set_progress(position / total * 100)
            if self.batch_stop_requested:
                break

        stopped = self.batch_stop_requested and processed < total
        remaining = total - processed
        self.root.after(
            0,
            lambda: self.finish_batch_archive(summary, stopped, remaining),
        )

    def finish_batch_archive(self, summary, stopped, remaining):
        self.batch_running = False
        self.batch_stop_requested = False
        self.set_batch_queue_enabled(True)
        self.set_enabled(True)
        self.btn_batch_stop.configure(state='disabled', text="Остановить после текущего")
        self.file_notebook.tab(self.single_tab, state='normal')

        completed = summary['ready'] + summary['warnings'] + summary['errors']
        if stopped:
            self.set_status(f"Пакет остановлен · обработано {completed}, осталось {remaining}")
        else:
            self.set_progress(100)
            self.set_status(
                f"Пакет завершён · готово {summary['ready']}, "
                f"предупреждений {summary['warnings']}, ошибок {summary['errors']}"
            )

        lines = [
            "Пакетная обработка остановлена." if stopped else "Пакетная обработка завершена.",
            "",
            f"Готово: {summary['ready']}",
            f"С предупреждениями: {summary['warnings']}",
            f"Ошибок: {summary['errors']}",
            f"Пропущено: {summary['skipped']}",
        ]
        if stopped:
            lines.append(f"Осталось в очереди: {remaining}")
        lines.extend(("", "Подробности находятся в журнале и _report.txt."))
        text = '\n'.join(lines)
        if stopped or summary['warnings'] or summary['errors']:
            messagebox.showwarning("Результат пакетной обработки", text)
        else:
            messagebox.showinfo("Пакет завершён", text)

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
    
    def log(self, msg, prefix=None):
        def update():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert('end', f"[{ts}] ", 'timestamp')
            display_msg = f"{prefix} — {msg}" if prefix else msg
            lowered = msg.lower()
            if 'ошиб' in lowered or msg.lstrip().startswith('✗'):
                tag = 'error'
            elif 'предуп' in lowered or 'отсутств' in lowered:
                tag = 'warning'
            elif 'готово' in lowered or msg.lstrip().startswith('+'):
                tag = 'success'
            else:
                tag = None
            self.log_text.insert('end', f"{display_msg}\n", tag)
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
        def update():
            self.btn_archive.configure(state=state)
            self.btn_analyze.configure(state=state)
            self.btn_batch_start.configure(state=state)
            self.organize_checkbutton.configure(state=state)
            self.convert_checkbutton.configure(state=state)
            self.btn_browse_max.configure(state=state)
            for checkbutton in self.category_checkbuttons:
                checkbutton.configure(state=state)
            if enabled:
                self.update_conversion_controls()
            else:
                self.max_install_combo.configure(state='disabled')
                self.target_version_combo.configure(state='disabled')
        self.root.after(0, update)
    
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
        scene_path = self.scene_var.get()
        archive_path = self.archive_var.get()
        if not scene_path:
            messagebox.showerror("Ошибка", "Выберите сцену!")
            return
        if not os.path.exists(scene_path):
            messagebox.showerror("Ошибка", "Файл не найден!")
            return
        if not archive_path:
            messagebox.showerror("Ошибка", "Укажите архив!")
            return
        conversion_settings = self.get_conversion_settings()
        if conversion_settings is None:
            return
        categories = {key: var.get() for key, var in self.cat_vars.items()}
        organize = self.organize_var.get()
        self.set_enabled(False)
        self.set_progress(0)
        threading.Thread(
            target=self.do_archive,
            args=(
                scene_path,
                archive_path,
                categories,
                organize,
                conversion_settings,
            ),
            daemon=True,
        ).start()

    def do_archive(
        self,
        scene_path,
        archive_path,
        categories,
        organize,
        conversion_settings,
    ):
        try:
            def update_progress(value):
                self.set_progress(value)
                if conversion_settings.enabled and value < 35:
                    self.set_status("Пересохранение сцены в 3ds Max...")
                else:
                    self.set_status("Архивация...")

            ok, count, size, stats = self.run_archive_job(
                scene_path,
                archive_path,
                categories,
                organize,
                conversion_settings,
                update_progress,
                self.log,
            )
            self.root.after(
                0,
                lambda: self.finish_single_archive(ok, count, size, stats),
            )
        except Exception as exc:
            self.log(f"ОШИБКА: {exc}")
            self.root.after(
                0,
                lambda message=str(exc): self.finish_single_exception(message),
            )

    def finish_single_archive(self, ok, count, size, stats):
        self.set_enabled(True)
        if ok and (
            stats['missing']
            or stats['errors']
            or stats.get('conversion_warnings')
        ):
            warning_lines = [
                "Архив создан, но не все ресурсы удалось добавить.",
                "",
                f"Ресурсов добавлено: {stats['resources']}",
                f"Отсутствует файлов: {stats['missing']}",
                f"Ошибок добавления: {len(stats['errors'])}",
                f"Предупреждений 3ds Max: {stats.get('conversion_warnings', 0)}",
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
        elif ok:
            self.set_status(f"Готово: {stats['resources']} ресурсов, {size:.1f} MB")
            conversion_line = ""
            if stats.get('conversion'):
                conversion_line = (
                    f"\nВерсия сцены: 3ds Max "
                    f"{stats['conversion']['target_version']}"
                )
            messagebox.showinfo(
                "Успех",
                f"Архив создан!\n\n"
                f"Ресурсов: {stats['resources']}\n"
                f"Файлов в архиве: {count}\n"
                f"Размер: {size:.1f} MB"
                f"{conversion_line}",
            )
        else:
            self.set_status("Ошибка")
            details = stats.get('errors', [])
            message = details[-1]['message'] if details else "Не удалось создать архив"
            messagebox.showerror("Ошибка", message)

    def finish_single_exception(self, message):
        self.set_enabled(True)
        self.set_status("Ошибка")
        messagebox.showerror("Ошибка", message)


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
