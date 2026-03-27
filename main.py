import base64
import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from cryptography.fernet import Fernet
from PyQt5.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "MEOW"
APP_DIR = Path.home() / ".meow_launcher"
SETTINGS_FILE = APP_DIR / "settings.json"
CREDENTIALS_FILE = APP_DIR / "credentials.enc"
GAME_DIR = APP_DIR / "game_profiles"

VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest.json"
MODRINTH_SEARCH_URL = "https://api.modrinth.com/v2/search"


@dataclass
class LauncherProfile:
    name: str
    mode: str
    minecraft_version: str
    loader: str
    loader_version: str
    memory_mb: int
    java_path: str = ""
    game_subdir: str = ""


@dataclass
class UserSettings:
    game_directory: str = str(GAME_DIR)
    default_memory_mb: int = 4096
    auto_check_updates: bool = True
    active_profile: str = "Vanilla 1.21"
    profiles: List[LauncherProfile] = field(default_factory=list)


class AppLogger:
    def __init__(self):
        self._listeners = []

    def subscribe(self, callback):
        self._listeners.append(callback)

    def info(self, message: str):
        self._emit("INFO", message)

    def warning(self, message: str):
        self._emit("WARN", message)

    def error(self, message: str):
        self._emit("ERROR", message)

    def _emit(self, level: str, message: str):
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        line = f"[{stamp}] [{level}] {message}"
        for callback in self._listeners:
            callback(line)


class SettingsStore:
    def __init__(self, logger: AppLogger):
        self.logger = logger
        APP_DIR.mkdir(parents=True, exist_ok=True)

    def _default_profiles(self, default_memory: int) -> List[LauncherProfile]:
        return [
            LauncherProfile(
                name="Vanilla 1.21",
                mode="VANILLA",
                minecraft_version="1.21.4",
                loader="none",
                loader_version="",
                memory_mb=default_memory,
                game_subdir="vanilla-1_21",
            ),
            LauncherProfile(
                name="Lite Fabric",
                mode="LITE",
                minecraft_version="1.21.4",
                loader="fabric",
                loader_version="latest",
                memory_mb=6144,
                game_subdir="lite-fabric",
            ),
            LauncherProfile(
                name="Forge Mods",
                mode="MODS",
                minecraft_version="1.20.1",
                loader="forge",
                loader_version="47.3.0",
                memory_mb=8192,
                game_subdir="mods-forge",
            ),
        ]

    def load(self) -> UserSettings:
        if not SETTINGS_FILE.exists():
            settings = UserSettings()
            settings.profiles = self._default_profiles(settings.default_memory_mb)
            self.save(settings)
            self.logger.info("Создан новый файл настроек")
            return settings

        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            profiles = [LauncherProfile(**p) for p in raw.get("profiles", [])]
            if not profiles:
                profiles = self._default_profiles(raw.get("default_memory_mb", 4096))
            settings = UserSettings(
                game_directory=raw.get("game_directory", str(GAME_DIR)),
                default_memory_mb=raw.get("default_memory_mb", 4096),
                auto_check_updates=raw.get("auto_check_updates", True),
                active_profile=raw.get("active_profile", profiles[0].name),
                profiles=profiles,
            )
            return settings
        except Exception as exc:
            self.logger.error(f"Ошибка чтения настроек: {exc}. Будут применены значения по умолчанию")
            settings = UserSettings()
            settings.profiles = self._default_profiles(settings.default_memory_mb)
            return settings

    def save(self, settings: UserSettings):
        payload = asdict(settings)
        SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class CryptoStore:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.key_file = APP_DIR / "key.bin"
        self.fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_file.exists():
            return self.key_file.read_bytes()
        key = Fernet.generate_key()
        self.key_file.write_bytes(key)
        return key

    def save_credentials(self, login: str, password: str) -> None:
        payload = json.dumps({"login": login, "password": password}).encode("utf-8")
        encrypted = self.fernet.encrypt(payload)
        CREDENTIALS_FILE.write_bytes(encrypted)

    def load_credentials(self):
        if not CREDENTIALS_FILE.exists():
            return None
        try:
            decoded = self.fernet.decrypt(CREDENTIALS_FILE.read_bytes())
            data = json.loads(decoded.decode("utf-8"))
            return data.get("login", ""), data.get("password", "")
        except Exception:
            return None


class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class APITask(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            data = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(data)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class DownloadSignals(QObject):
    progress = pyqtSignal(int, str)
    file_done = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)


class DownloadTask(QRunnable):
    def __init__(self, profile: LauncherProfile, game_root: Path, logger: AppLogger):
        super().__init__()
        self.profile = profile
        self.game_root = game_root
        self.logger = logger
        self.signals = DownloadSignals()

    def _download(self, url: str, target: Path):
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        target.write_bytes(response.content)

    def run(self):
        try:
            profile_dir = self.game_root / self.profile.game_subdir
            profile_dir.mkdir(parents=True, exist_ok=True)

            urls = [VERSION_MANIFEST_URL]
            if self.profile.mode in {"LITE", "MODS"}:
                facets = "[[\"project_type:mod\"],[\"categories:fabric\"]]" if self.profile.loader == "fabric" else "[[\"project_type:mod\"]]"
                mods_resp = requests.get(
                    MODRINTH_SEARCH_URL,
                    params={"query": "performance", "limit": 5, "facets": facets},
                    timeout=30,
                )
                mods_resp.raise_for_status()
                urls.append(mods_resp.url)

            for index, url in enumerate(urls, start=1):
                filename = base64.urlsafe_b64encode(url.encode()).decode()[:32] + ".cache"
                target = profile_dir / filename
                self._download(url, target)
                pct = int((index / len(urls)) * 100)
                self.signals.progress.emit(pct, f"Загрузка {target.name}")
                self.signals.file_done.emit(target.name)

            launch_script = profile_dir / "launch_args.json"
            payload = {
                "profile": asdict(self.profile),
                "generated_utc": datetime.utcnow().isoformat() + "Z",
                "jvm_args": [f"-Xmx{self.profile.memory_mb}M", "-XX:+UseG1GC"],
                "game_args": ["--quickPlaySingleplayer", "World"],
            }
            launch_script.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.signals.finished.emit(str(launch_script))
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class GlassCard(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, title: str, subtitle: str, accent: str):
        super().__init__()
        self.title = title
        self.accent = accent
        self.setObjectName("glassCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(150)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 235, 255, 70))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        title_lbl = QLabel(title)
        title_lbl.setFont(QFont("Segoe UI", 16, QFont.Bold))
        subtitle_lbl = QLabel(subtitle)
        subtitle_lbl.setWordWrap(True)
        subtitle_lbl.setFont(QFont("Segoe UI", 10))

        badge = QLabel("Рекомендуется")
        badge.setStyleSheet(f"background:{accent}; color:#00181e; border-radius:10px; padding:4px 10px; font-weight:700;")
        badge.setFixedWidth(120)

        layout.addWidget(title_lbl)
        layout.addWidget(subtitle_lbl)
        layout.addWidget(badge)
        layout.addStretch()

        self.anim = QPropertyAnimation(self, b"minimumHeight")
        self.anim.setDuration(180)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def enterEvent(self, event):
        self.anim.stop()
        self.anim.setStartValue(self.minimumHeight())
        self.anim.setEndValue(170)
        self.anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.anim.stop()
        self.anim.setStartValue(self.minimumHeight())
        self.anim.setEndValue(150)
        self.anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit(self.title)
        super().mousePressEvent(event)


class LoginPage(QWidget):
    login_success = pyqtSignal(str)

    def __init__(self, crypto: CryptoStore):
        super().__init__()
        self.crypto = crypto

        root = QVBoxLayout(self)
        root.addStretch()

        box = QFrame()
        box.setObjectName("loginBox")
        box.setFixedWidth(440)
        form = QVBoxLayout(box)
        form.setContentsMargins(28, 28, 28, 28)

        title = QLabel("MEOW Launcher")
        title.setFont(QFont("Segoe UI", 24, QFont.Bold))
        subtitle = QLabel("Современный лаунчер с профилями, модами и умной загрузкой")
        subtitle.setWordWrap(True)

        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Логин")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Пароль")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox("Запомнить меня")

        btn = QPushButton("Войти")
        btn.clicked.connect(self.try_login)

        self.info = QLabel("")
        self.info.setObjectName("infoText")

        form.addWidget(title)
        form.addWidget(subtitle)
        form.addWidget(self.login_edit)
        form.addWidget(self.password_edit)
        form.addWidget(self.remember)
        form.addWidget(btn)
        form.addWidget(self.info)

        root.addWidget(box, alignment=Qt.AlignCenter)
        root.addStretch()

        saved = self.crypto.load_credentials()
        if saved:
            self.login_edit.setText(saved[0])
            self.password_edit.setText(saved[1])
            self.remember.setChecked(True)

    def try_login(self):
        login = self.login_edit.text().strip()
        password = self.password_edit.text().strip()
        if not login or not password:
            self.info.setText("Введите логин и пароль")
            return
        if self.remember.isChecked():
            self.crypto.save_credentials(login, password)
        self.info.setText("Вход выполнен")
        self.login_success.emit(login)


class MainPage(QWidget):
    def __init__(self, logger: AppLogger):
        super().__init__()
        self.logger = logger
        self.thread_pool = QThreadPool.globalInstance()
        self.settings_store = SettingsStore(logger)
        self.settings = self.settings_store.load()
        self.current_user = ""

        self._ensure_game_directories()

        self.root = QHBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)

        self.sidebar = self._build_sidebar()
        self.stack = QStackedWidget()

        self.home_page = self._build_home_page()
        self.profiles_page = self._build_profiles_page()
        self.discover_page = self._build_discover_page()
        self.logs_page = self._build_logs_page()
        self.settings_page = self._build_settings_page()

        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.profiles_page)
        self.stack.addWidget(self.discover_page)
        self.stack.addWidget(self.logs_page)
        self.stack.addWidget(self.settings_page)

        self.root.addWidget(self.sidebar, 1)
        self.root.addWidget(self.stack, 4)

        self.nav_list.setCurrentRow(0)
        self.refresh_profile_list()
        self._sync_home_with_active_profile()

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._update_clock)
        self.log_timer.start(1000)

    def _ensure_game_directories(self):
        Path(self.settings.game_directory).mkdir(parents=True, exist_ok=True)
        for profile in self.settings.profiles:
            (Path(self.settings.game_directory) / profile.game_subdir).mkdir(parents=True, exist_ok=True)

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidePanel")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("MEOW")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        subtitle = QLabel("Launcher Control Center")
        subtitle.setStyleSheet("color: #8fdce5")

        self.nav_list = QListWidget()
        self.nav_list.addItems(["Главная", "Профили", "Каталог модов", "Логи", "Настройки"])
        self.nav_list.currentRowChanged.connect(self.stack.setCurrentIndex)

        self.clock = QLabel("UTC --:--:--")
        self.clock.setStyleSheet("color:#7ec7d0")

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.nav_list)
        layout.addStretch()
        layout.addWidget(self.clock)
        return sidebar

    def _build_home_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel("Быстрый запуск")
        header.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.user_label = QLabel("Пользователь: -")

        cards = QHBoxLayout()
        self.mode_cards = {}
        cards_data = [
            ("VANILLA", "Чистая игра и минимальный риск конфликтов", "#66fff2"),
            ("LITE", "Оптимизация FPS + качественные utility моды", "#38e7ff"),
            ("MODS", "Тяжелые сборки, Forge/Fabric профили", "#3a93ff"),
        ]
        for mode, text, color in cards_data:
            card = GlassCard(mode, text, color)
            card.clicked.connect(self._choose_mode_from_card)
            cards.addWidget(card)
            self.mode_cards[mode] = card

        self.active_profile_label = QLabel("Активный профиль: -")
        self.launch_btn = QPushButton("Подготовить запуск")
        self.launch_btn.setObjectName("launchButton")
        self.launch_btn.clicked.connect(self.start_profile_prepare)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.status = QLabel("Готов")

        layout.addWidget(header)
        layout.addWidget(self.user_label)
        layout.addLayout(cards)
        layout.addWidget(self.active_profile_label)
        layout.addWidget(self.launch_btn)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        return page

    def _build_profiles_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)

        left = QVBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self.on_profile_selected)
        left.addWidget(QLabel("Профили"))
        left.addWidget(self.profile_list)

        right = QFrame()
        right.setObjectName("glassCard")
        form = QFormLayout(right)

        self.profile_name_edit = QLineEdit()
        self.profile_mode_edit = QLineEdit()
        self.profile_mc_edit = QLineEdit()
        self.profile_loader_edit = QLineEdit()
        self.profile_loader_ver_edit = QLineEdit()
        self.profile_memory_spin = QSpinBox()
        self.profile_memory_spin.setRange(2048, 32768)
        self.profile_memory_spin.setSingleStep(512)

        form.addRow("Имя", self.profile_name_edit)
        form.addRow("Режим", self.profile_mode_edit)
        form.addRow("Minecraft", self.profile_mc_edit)
        form.addRow("Лоадер", self.profile_loader_edit)
        form.addRow("Версия лоадера", self.profile_loader_ver_edit)
        form.addRow("RAM (MB)", self.profile_memory_spin)

        buttons = QHBoxLayout()
        save_btn = QPushButton("Сохранить")
        save_btn.clicked.connect(self.save_profile_changes)
        new_btn = QPushButton("Новый профиль")
        new_btn.clicked.connect(self.create_profile)
        activate_btn = QPushButton("Сделать активным")
        activate_btn.clicked.connect(self.activate_selected_profile)
        buttons.addWidget(save_btn)
        buttons.addWidget(new_btn)
        buttons.addWidget(activate_btn)

        wrapper = QVBoxLayout()
        wrapper.addWidget(right)
        wrapper.addLayout(buttons)
        wrapper.addStretch()

        layout.addLayout(left, 2)
        layout.addLayout(wrapper, 3)
        return page

    def _build_discover_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)

        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск модов на Modrinth")
        self.search_btn = QPushButton("Искать")
        self.search_btn.clicked.connect(self.search_mods)
        top.addWidget(self.search_edit)
        top.addWidget(self.search_btn)

        self.discover_info = QLabel("Нажмите «Искать», чтобы получить подборку")
        self.discover_results = QListWidget()

        layout.addLayout(top)
        layout.addWidget(self.discover_info)
        layout.addWidget(self.discover_results)
        return page

    def _build_logs_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        clear_btn = QPushButton("Очистить лог")
        clear_btn.clicked.connect(self.log_output.clear)
        layout.addWidget(QLabel("События лаунчера"))
        layout.addWidget(self.log_output)
        layout.addWidget(clear_btn)
        self.logger.subscribe(self._append_log)
        return page

    def _build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)

        panel = QFrame()
        panel.setObjectName("glassCard")
        form = QFormLayout(panel)

        self.game_dir_label = QLabel(self.settings.game_directory)
        choose_btn = QPushButton("Изменить папку")
        choose_btn.clicked.connect(self.pick_directory)

        dir_row = QHBoxLayout()
        dir_row.addWidget(self.game_dir_label)
        dir_row.addWidget(choose_btn)

        dir_wrapper = QWidget()
        dir_wrapper.setLayout(dir_row)

        self.default_ram_slider = QSlider(Qt.Horizontal)
        self.default_ram_slider.setRange(2048, 32768)
        self.default_ram_slider.setSingleStep(512)
        self.default_ram_slider.setValue(self.settings.default_memory_mb)
        self.default_ram_slider.valueChanged.connect(self.memory_changed)
        self.default_ram_label = QLabel(f"{self.settings.default_memory_mb} MB")

        ram_row = QHBoxLayout()
        ram_row.addWidget(self.default_ram_slider)
        ram_row.addWidget(self.default_ram_label)
        ram_wrapper = QWidget()
        ram_wrapper.setLayout(ram_row)

        self.java_status = QLabel("Java: не проверено")
        java_btn = QPushButton("Проверить JAVA_HOME")
        java_btn.clicked.connect(self.check_java)

        form.addRow("Директория", dir_wrapper)
        form.addRow("RAM по умолчанию", ram_wrapper)
        form.addRow("Java", self.java_status)
        form.addRow("", java_btn)

        layout.addWidget(panel)
        layout.addStretch()
        return page

    def set_user(self, username: str):
        self.current_user = username
        self.user_label.setText(f"Пользователь: {username}")
        self.logger.info(f"Пользователь {username} вошел в систему")

    def _append_log(self, line: str):
        self.log_output.append(line)

    def _update_clock(self):
        self.clock.setText(datetime.utcnow().strftime("UTC %H:%M:%S"))

    def refresh_profile_list(self):
        self.profile_list.clear()
        for profile in self.settings.profiles:
            item = QListWidgetItem(f"{profile.name} [{profile.mode}]")
            item.setData(Qt.UserRole, profile.name)
            self.profile_list.addItem(item)

        active_index = 0
        for i, profile in enumerate(self.settings.profiles):
            if profile.name == self.settings.active_profile:
                active_index = i
                break
        if self.settings.profiles:
            self.profile_list.setCurrentRow(active_index)

    def get_profile(self, profile_name: str) -> Optional[LauncherProfile]:
        for profile in self.settings.profiles:
            if profile.name == profile_name:
                return profile
        return None

    def on_profile_selected(self, current, _previous):
        if not current:
            return
        profile_name = current.data(Qt.UserRole)
        profile = self.get_profile(profile_name)
        if not profile:
            return
        self.profile_name_edit.setText(profile.name)
        self.profile_mode_edit.setText(profile.mode)
        self.profile_mc_edit.setText(profile.minecraft_version)
        self.profile_loader_edit.setText(profile.loader)
        self.profile_loader_ver_edit.setText(profile.loader_version)
        self.profile_memory_spin.setValue(profile.memory_mb)

    def save_profile_changes(self):
        current = self.profile_list.currentItem()
        if not current:
            return
        old_name = current.data(Qt.UserRole)
        profile = self.get_profile(old_name)
        if not profile:
            return

        profile.name = self.profile_name_edit.text().strip() or old_name
        profile.mode = self.profile_mode_edit.text().strip().upper() or profile.mode
        profile.minecraft_version = self.profile_mc_edit.text().strip() or profile.minecraft_version
        profile.loader = self.profile_loader_edit.text().strip() or profile.loader
        profile.loader_version = self.profile_loader_ver_edit.text().strip() or profile.loader_version
        profile.memory_mb = self.profile_memory_spin.value()

        profile.game_subdir = profile.game_subdir or profile.name.lower().replace(" ", "-")

        if self.settings.active_profile == old_name:
            self.settings.active_profile = profile.name

        self.settings_store.save(self.settings)
        self.refresh_profile_list()
        self._sync_home_with_active_profile()
        self.logger.info(f"Профиль {profile.name} сохранен")

    def create_profile(self):
        base = LauncherProfile(
            name=f"Custom {len(self.settings.profiles) + 1}",
            mode="MODS",
            minecraft_version="1.21.4",
            loader="fabric",
            loader_version="latest",
            memory_mb=self.settings.default_memory_mb,
            game_subdir=f"custom-{len(self.settings.profiles) + 1}",
        )
        self.settings.profiles.append(base)
        self.settings_store.save(self.settings)
        self.refresh_profile_list()
        self.logger.info(f"Создан новый профиль {base.name}")

    def activate_selected_profile(self):
        current = self.profile_list.currentItem()
        if not current:
            return
        profile_name = current.data(Qt.UserRole)
        self.settings.active_profile = profile_name
        self.settings_store.save(self.settings)
        self._sync_home_with_active_profile()
        self.logger.info(f"Активирован профиль {profile_name}")

    def _sync_home_with_active_profile(self):
        profile = self.get_profile(self.settings.active_profile)
        if not profile:
            return
        self.active_profile_label.setText(
            f"Активный профиль: {profile.name} | MC {profile.minecraft_version} | {profile.loader}"
        )
        self.launch_btn.setText(f"Подготовить {profile.name}")
        self._apply_launch_color(profile.mode)

    def _choose_mode_from_card(self, mode: str):
        target = None
        for profile in self.settings.profiles:
            if profile.mode == mode:
                target = profile.name
                break
        if target:
            self.settings.active_profile = target
            self.settings_store.save(self.settings)
            self._sync_home_with_active_profile()
            self.logger.info(f"Выбран режим {mode} через карточку")

    def _apply_launch_color(self, mode: str):
        color_map = {"VANILLA": "#66fff2", "LITE": "#30e4ff", "MODS": "#428bff"}
        color = color_map.get(mode, "#66fff2")
        self.launch_btn.setStyleSheet(
            f"QPushButton#launchButton {{background:{color}; color:#03232a; font-weight:700; border-radius:14px; padding:12px;}}"
        )

    def start_profile_prepare(self):
        profile = self.get_profile(self.settings.active_profile)
        if not profile:
            self.status.setText("Профиль не выбран")
            return
        self.progress.setValue(0)
        self.status.setText("Подготовка файлов...")

        task = DownloadTask(profile, Path(self.settings.game_directory), self.logger)
        task.signals.progress.connect(self.on_download_progress)
        task.signals.file_done.connect(self.on_file_done)
        task.signals.finished.connect(self.on_download_finished)
        task.signals.failed.connect(self.on_download_failed)
        self.thread_pool.start(task)
        self.logger.info(f"Начата подготовка профиля {profile.name}")

    def on_download_progress(self, percent: int, text: str):
        self.progress.setValue(percent)
        self.status.setText(text)

    def on_file_done(self, filename: str):
        self.status.setText(f"Скачано: {filename}")

    def on_download_finished(self, launch_file: str):
        self.progress.setValue(100)
        self.status.setText("Готово к запуску")
        self.logger.info(f"Профиль подготовлен. Конфигурация: {launch_file}")

    def on_download_failed(self, error: str):
        self.status.setText("Ошибка подготовки")
        self.logger.error(error.splitlines()[-1] if error else "unknown error")

    def search_mods(self):
        query = self.search_edit.text().strip() or "optimization"
        self.discover_info.setText("Идет поиск...")
        self.discover_results.clear()

        def fetch_mods():
            response = requests.get(
                MODRINTH_SEARCH_URL,
                params={
                    "query": query,
                    "limit": 15,
                    "facets": '[["project_type:mod"]]',
                    "index": "relevance",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            hits = data.get("hits", [])
            result = []
            for hit in hits:
                result.append(
                    {
                        "title": hit.get("title", "Без названия"),
                        "downloads": hit.get("downloads", 0),
                        "followers": hit.get("follows", 0),
                        "description": hit.get("description", "").strip(),
                    }
                )
            return result

        task = APITask(fetch_mods)
        task.signals.finished.connect(self.on_mods_loaded)
        task.signals.failed.connect(self.on_mods_failed)
        self.thread_pool.start(task)

    def on_mods_loaded(self, mods: List[Dict]):
        if not mods:
            self.discover_info.setText("Ничего не найдено")
            return
        for mod in mods:
            line = (
                f"{mod['title']} | downloads: {mod['downloads']} | follows: {mod['followers']}\n"
                f"{mod['description']}"
            )
            self.discover_results.addItem(line)
        self.discover_info.setText(f"Найдено модов: {len(mods)}")
        self.logger.info(f"Каталог обновлен, получено {len(mods)} записей")

    def on_mods_failed(self, error: str):
        self.discover_info.setText("Ошибка загрузки каталога")
        self.logger.error(f"Modrinth API: {error}")

    def pick_directory(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите директорию")
        if not folder:
            return
        self.settings.game_directory = folder
        self.game_dir_label.setText(folder)
        self._ensure_game_directories()
        self.settings_store.save(self.settings)
        self.logger.info(f"Изменена директория игры: {folder}")

    def memory_changed(self, value):
        snapped = value - (value % 512)
        self.settings.default_memory_mb = snapped
        self.default_ram_label.setText(f"{snapped} MB")
        self.settings_store.save(self.settings)

    def check_java(self):
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            self.java_status.setText(f"Java: найдено ({java_home})")
        else:
            self.java_status.setText("Java: не найдено, установите JRE/JDK")


class FramelessWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.logger = AppLogger()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(1280, 820)
        self.drag_pos = QPoint()

        wrapper = QWidget()
        root = QVBoxLayout(wrapper)
        root.setContentsMargins(12, 12, 12, 12)

        titlebar = QFrame()
        titlebar.setObjectName("titlebar")
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(10, 6, 10, 6)

        title = QLabel("MEOW Launcher")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.subtitle = QLabel("next-gen minecraft launcher")
        self.subtitle.setStyleSheet("color:#8fdce5")

        minimize = QPushButton("—")
        minimize.clicked.connect(self.showMinimized)
        close = QPushButton("✕")
        close.clicked.connect(self.close)
        for button in (minimize, close):
            button.setFixedSize(36, 28)
            button.setObjectName("windowBtn")

        title_layout.addWidget(title)
        title_layout.addWidget(self.subtitle)
        title_layout.addStretch()
        title_layout.addWidget(minimize)
        title_layout.addWidget(close)

        self.stack = QStackedWidget()
        self.crypto = CryptoStore()
        self.login_page = LoginPage(self.crypto)
        self.main_page = MainPage(self.logger)

        self.login_page.login_success.connect(self.open_main)

        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.main_page)

        root.addWidget(titlebar)
        root.addWidget(self.stack)
        self.setCentralWidget(wrapper)

        self.apply_styles()

    def open_main(self, username: str):
        self.main_page.set_user(username)
        self.stack.setCurrentWidget(self.main_page)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and not self.drag_pos.isNull():
            self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def apply_styles(self):
        self.setStyleSheet(
            """
            QWidget {
                background-color: rgba(14, 23, 38, 235);
                color: #ddffff;
                font-family: Segoe UI;
                font-size: 13px;
            }
            #titlebar, #loginBox, #sidePanel, #glassCard {
                background-color: rgba(255, 255, 255, 22);
                border: 1px solid rgba(102, 255, 242, 95);
                border-radius: 16px;
            }
            QListWidget {
                background: rgba(5, 15, 25, 80);
                border-radius: 12px;
                padding: 8px;
            }
            QListWidget::item {
                padding: 10px;
                border-radius: 8px;
            }
            QListWidget::item:selected {
                background: rgba(56, 212, 255, 90);
            }
            QLineEdit, QTextEdit, QSpinBox {
                background: rgba(0, 0, 0, 55);
                border-radius: 12px;
                border: 1px solid rgba(91, 255, 243, 90);
                padding: 8px;
            }
            QPushButton {
                background-color: rgba(40, 240, 255, 185);
                color: #042329;
                border: none;
                border-radius: 12px;
                padding: 10px;
                font-weight: 600;
            }
            QPushButton#windowBtn {
                background-color: rgba(250, 255, 255, 55);
                color: #adffff;
            }
            QLabel#infoText { color: #89ffb8; }
            QProgressBar {
                border-radius: 10px;
                background: rgba(0, 0, 0, 80);
                border: 1px solid rgba(120, 255, 250, 90);
                height: 24px;
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 10px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00c8ff,
                    stop:1 #66fff2
                );
            }
            """
        )


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = FramelessWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
