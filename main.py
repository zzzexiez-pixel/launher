import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

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
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "MEOW"
APP_DIR = Path.home() / ".meow_launcher"
SETTINGS_FILE = APP_DIR / "settings.json"
CREDENTIALS_FILE = APP_DIR / "credentials.enc"
GAME_DIR = APP_DIR / "game_profiles"

DOWNLOAD_TARGETS = {
    "VANILLA": [
        "https://piston-meta.mojang.com/mc/game/version_manifest.json",
    ],
    "LITE": [
        "https://api.modrinth.com/v2/project/sodium",
        "https://api.modrinth.com/v2/project/lithium",
        "https://api.modrinth.com/v2/project/phosphor",
    ],
    "MODS": [
        "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml",
        "https://meta.fabricmc.net/v2/versions/loader",
    ],
}


@dataclass
class UserSettings:
    game_directory: str = str(GAME_DIR)
    memory_mb: int = 4096


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


class DownloadSignals(QObject):
    progress = pyqtSignal(int, str)
    file_done = pyqtSignal(str)
    finished = pyqtSignal()
    failed = pyqtSignal(str)


class DownloaderTask(QRunnable):
    def __init__(self, urls, mode_folder: Path):
        super().__init__()
        self.urls = urls
        self.mode_folder = mode_folder
        self.signals = DownloadSignals()

    def run(self):
        try:
            self.mode_folder.mkdir(parents=True, exist_ok=True)
            total = len(self.urls)
            for i, url in enumerate(self.urls, start=1):
                file_name = base64.urlsafe_b64encode(url.encode()).decode()[:32] + ".dat"
                target = self.mode_folder / file_name
                response = requests.get(url, timeout=20)
                response.raise_for_status()
                target.write_bytes(response.content)
                pct = int((i / total) * 100)
                self.signals.progress.emit(pct, f"Загрузка: {target.name}")
                self.signals.file_done.emit(target.name)
            self.signals.finished.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class GlassCard(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, title: str, subtitle: str, color: str):
        super().__init__()
        self.title = title
        self.base_color = color
        self.setObjectName("glassCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(180)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 255, 230, 80))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        name = QLabel(title)
        name.setFont(QFont("Segoe UI", 18, QFont.Bold))
        desc = QLabel(subtitle)
        desc.setWordWrap(True)
        desc.setFont(QFont("Segoe UI", 10))
        layout.addWidget(name)
        layout.addWidget(desc)
        layout.addStretch()

        self.anim = QPropertyAnimation(self, b"minimumHeight")
        self.anim.setDuration(180)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def enterEvent(self, event):
        self.anim.stop()
        self.anim.setStartValue(self.height())
        self.anim.setEndValue(195)
        self.anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.anim.stop()
        self.anim.setStartValue(self.height())
        self.anim.setEndValue(180)
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
        box = QFrame()
        box.setObjectName("loginBox")
        layout = QVBoxLayout(self)
        layout.addStretch()
        layout.addWidget(box, alignment=Qt.AlignCenter)
        layout.addStretch()

        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(30, 30, 30, 30)

        title = QLabel("MEOW Launcher")
        title.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Логин")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Пароль")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox("Запомнить меня")

        self.info = QLabel("")
        self.info.setObjectName("infoText")
        login_btn = QPushButton("Войти")
        login_btn.clicked.connect(self.try_login)

        box_layout.addWidget(title)
        box_layout.addWidget(self.login_edit)
        box_layout.addWidget(self.password_edit)
        box_layout.addWidget(self.remember)
        box_layout.addWidget(login_btn)
        box_layout.addWidget(self.info)

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
        self.info.setText("Успешный вход")
        self.login_success.emit(login)


class MainPage(QWidget):
    def __init__(self):
        super().__init__()
        self.thread_pool = QThreadPool.globalInstance()
        self.settings = self.load_settings()
        self.selected_mode = "VANILLA"

        root = QHBoxLayout(self)

        left = QFrame()
        left.setObjectName("sidePanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(18, 18, 18, 18)

        self.dir_label = QLabel(f"Директория: {self.settings.game_directory}")
        choose_dir = QPushButton("Выбрать папку")
        choose_dir.clicked.connect(self.pick_directory)

        self.memory_label = QLabel(f"RAM: {self.settings.memory_mb} MB")
        self.memory_slider = QSlider(Qt.Horizontal)
        self.memory_slider.setRange(2048, 16384)
        self.memory_slider.setSingleStep(512)
        self.memory_slider.setValue(self.settings.memory_mb)
        self.memory_slider.valueChanged.connect(self.memory_changed)

        self.java_status = QLabel("Java: не проверено")
        java_btn = QPushButton("Проверить Java")
        java_btn.clicked.connect(self.check_java)

        left_layout.addWidget(self.dir_label)
        left_layout.addWidget(choose_dir)
        left_layout.addWidget(self.memory_label)
        left_layout.addWidget(self.memory_slider)
        left_layout.addWidget(self.java_status)
        left_layout.addWidget(java_btn)
        left_layout.addStretch()

        center = QWidget()
        center_layout = QVBoxLayout(center)

        cards_data = [
            ("VANILLA", "Чистая версия Minecraft без модов", "#66fff2"),
            ("LITE", "Оптимизация FPS: Sodium, Lithium, Phosphor", "#3de3ff"),
            ("MODS", "Forge/Fabric и управление сборками", "#13bdff"),
        ]

        self.cards = []
        for title, subtitle, color in cards_data:
            card = GlassCard(title, subtitle, color)
            card.clicked.connect(self.select_mode)
            center_layout.addWidget(card)
            self.cards.append(card)

        self.launch_btn = QPushButton("Запустить VANILLA")
        self.launch_btn.setObjectName("launchButton")
        self.launch_btn.clicked.connect(self.start_download)
        self.status_label = QLabel("Готов к запуску")
        self.progress = QProgressBar()
        self.progress.setValue(0)

        center_layout.addWidget(self.launch_btn)
        center_layout.addWidget(self.status_label)
        center_layout.addWidget(self.progress)

        root.addWidget(left, 1)
        root.addWidget(center, 2)

        self.ensure_mode_folders()
        self.apply_launch_color()

    def ensure_mode_folders(self):
        for mode in ("vanilla", "lite", "mods"):
            Path(self.settings.game_directory, mode).mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> UserSettings:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return UserSettings(**data)
        settings = UserSettings()
        SETTINGS_FILE.write_text(json.dumps(settings.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        return settings

    def save_settings(self):
        SETTINGS_FILE.write_text(json.dumps(self.settings.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    def pick_directory(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите директорию")
        if folder:
            self.settings.game_directory = folder
            self.dir_label.setText(f"Директория: {folder}")
            self.ensure_mode_folders()
            self.save_settings()

    def memory_changed(self, value):
        snapped = value - (value % 512)
        self.settings.memory_mb = snapped
        self.memory_label.setText(f"RAM: {snapped} MB")
        self.save_settings()

    def check_java(self):
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            self.java_status.setText(f"Java: найдено ({java_home})")
        else:
            self.java_status.setText("Java: не найдено, установите JRE/JDK")

    def select_mode(self, mode: str):
        self.selected_mode = mode
        self.launch_btn.setText(f"Запустить {mode}")
        self.status_label.setText(f"Выбран режим: {mode}")
        self.apply_launch_color()

    def apply_launch_color(self):
        color_map = {"VANILLA": "#66fff2", "LITE": "#20d4ff", "MODS": "#1477ff"}
        color = color_map.get(self.selected_mode, "#66fff2")
        self.launch_btn.setStyleSheet(
            f"QPushButton#launchButton {{background:{color}; color:#022; font-weight:700; border-radius:14px; padding:12px;}}"
        )

    def start_download(self):
        mode_dir = Path(self.settings.game_directory) / self.selected_mode.lower()
        urls = DOWNLOAD_TARGETS[self.selected_mode]
        self.progress.setValue(0)
        self.status_label.setText("Подготовка к загрузке...")

        self.task = DownloaderTask(urls, mode_dir)
        self.task.signals.progress.connect(self.on_progress)
        self.task.signals.file_done.connect(self.on_file_done)
        self.task.signals.finished.connect(self.on_finished)
        self.task.signals.failed.connect(self.on_failed)
        self.thread_pool.start(self.task)

    def on_progress(self, percent: int, text: str):
        self.progress.setValue(percent)
        self.status_label.setText(text)

    def on_file_done(self, filename: str):
        self.status_label.setText(f"Файл загружен: {filename}")

    def on_finished(self):
        self.progress.setValue(100)
        self.status_label.setText(f"{self.selected_mode}: готово к запуску")

    def on_failed(self, error: str):
        self.status_label.setText(f"Ошибка: {error}")


class FramelessWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(1150, 720)
        self.drag_pos = QPoint()

        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(12, 12, 12, 12)

        titlebar = QFrame()
        titlebar.setObjectName("titlebar")
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(10, 6, 10, 6)

        title = QLabel("MEOW")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        minimize = QPushButton("—")
        minimize.clicked.connect(self.showMinimized)
        close = QPushButton("✕")
        close.clicked.connect(self.close)

        for btn in (minimize, close):
            btn.setFixedSize(36, 28)
            btn.setObjectName("windowBtn")

        title_layout.addWidget(title)
        title_layout.addStretch()
        title_layout.addWidget(minimize)
        title_layout.addWidget(close)

        self.stack = QStackedWidget()
        self.crypto = CryptoStore()
        self.login_page = LoginPage(self.crypto)
        self.main_page = MainPage()
        self.login_page.login_success.connect(self.open_main)

        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.main_page)

        layout.addWidget(titlebar)
        layout.addWidget(self.stack)
        self.setCentralWidget(wrapper)

        self.apply_styles()

    def open_main(self, _username: str):
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
                background-color: rgba(15, 24, 38, 235);
                color: #ddffff;
                font-family: Segoe UI;
            }
            #titlebar, #loginBox, #sidePanel, #glassCard {
                background-color: rgba(255, 255, 255, 22);
                border: 1px solid rgba(102, 255, 242, 95);
                border-radius: 16px;
            }
            QLineEdit, QSlider, QPushButton, QCheckBox {
                margin-top: 8px;
            }
            QLineEdit {
                background: rgba(0, 0, 0, 55);
                border-radius: 12px;
                border: 1px solid rgba(91, 255, 243, 90);
                padding: 8px;
            }
            QPushButton {
                background-color: rgba(40, 240, 255, 185);
                color: #052226;
                border: none;
                border-radius: 12px;
                padding: 10px;
                font-weight: 600;
            }
            QPushButton#windowBtn {
                background-color: rgba(250, 255, 255, 55);
                color: #adffff;
            }
            QLabel#infoText {
                color: #89ffb8;
            }
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
    window = FramelessWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
