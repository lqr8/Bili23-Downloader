from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QTextEdit
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QTextCharFormat, QColor, QTextCursor

from qfluentwidgets import LineEdit, PushButton, PlainTextEdit, isDarkTheme

from gui.component.dialog import FluentWidget
from gui.component.widget import TipLabel

from util.common.config import config
from util.common.enum import ToastNotificationCategory
from util.common.io.directory import Directory
from util.common.signal_bus import signal_bus
from util.common.translator import Translator
from util.download.task.info import TaskInfo
from util.summary.worker import SummaryWorker, get_summary_path

from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class TextViewerDialog(FluentWidget):
    """内置文本查看器，用于查看语音转文字结果与 AI 总结。

    file_path 为 None 或文件不存在时显示占位提示（仅 AI 总结允许重新生成）。
    """

    # 搜索高亮的最大匹配数：转写文本可能很长，常见关键字会产生大量匹配，
    # 不限制时每个匹配都创建一个 ExtraSelection，会导致界面卡顿
    MAX_SEARCH_MATCHES = 1000

    def __init__(self, file_path: Path | None, task_info: TaskInfo = None, is_summary: bool = False, parent = None):
        super().__init__(parent = parent)

        self.file_path = file_path
        self.task_info = task_info
        self.is_summary = is_summary

        self._summary_worker = None

        self.setWindowTitle(file_path.name if file_path else (task_info.Basic.show_title if task_info else ""))
        self.setMinimumSize(800, 520)

        self.init_UI()
        self.load_content()

        self._init_common()

        # 查看器可同时打开多个，关闭时销毁自身
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def init_UI(self):
        self.search_box = LineEdit(self)
        self.search_box.setMinimumWidth(250)
        self.search_box.setPlaceholderText(self.tr("Search content..."))
        self.search_box.setClearButtonEnabled(True)

        self.open_dir_btn = PushButton(self.tr("Open File Location"), self)

        self.regenerate_btn = PushButton(self.tr("Regenerate"), self)

        self.text_box = PlainTextEdit(self)
        self.text_box.setReadOnly(True)
        self.text_box.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        tip_label = TipLabel(
            self.tr("Tips: Enter keywords in the search box to highlight matches"), self
        )

        top_layout = QHBoxLayout()
        top_layout.addWidget(self.search_box)
        top_layout.addWidget(self.open_dir_btn)
        top_layout.addWidget(self.regenerate_btn)
        top_layout.addStretch()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, self.titleBar.height(), 15, 15)
        main_layout.addLayout(top_layout)
        main_layout.addSpacing(10)
        main_layout.addWidget(self.text_box)
        main_layout.addSpacing(10)
        main_layout.addWidget(tip_label)

        # 只有 AI 总结视图允许重新生成
        self.regenerate_btn.setVisible(self.is_summary and self.task_info is not None)

        self.connect_signals()

    def connect_signals(self):
        self.search_box.textChanged.connect(self.on_search_changed)
        self.open_dir_btn.clicked.connect(self.open_file_location)
        self.regenerate_btn.clicked.connect(self.regenerate_summary)

    def load_content(self):
        if self.file_path is not None and self.file_path.exists():
            self.text_box.setPlainText(self.file_path.read_text(encoding = "utf-8", errors = "replace"))
        else:
            if self.is_summary:
                self.text_box.setPlainText(self.tr("The AI summary has not been generated yet. Click \"Regenerate\" to generate it."))
            else:
                self.text_box.setPlainText(self.tr("The file does not exist. It may have been moved or deleted."))

    def on_search_changed(self, text: str):
        # 高亮所有匹配项，并将光标移动到第一个匹配处
        extra_selections = []

        if text:
            if isDarkTheme():
                match_color = QColor(255, 255, 255, 60)
            else:
                match_color = QColor(0, 120, 215, 60)

            cursor = self.text_box.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)

            while len(extra_selections) < self.MAX_SEARCH_MATCHES:
                cursor = self.text_box.document().find(text, cursor)

                if cursor.isNull():
                    break

                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = QTextCharFormat()
                selection.format.setBackground(match_color)

                extra_selections.append(selection)

            self.text_box.setExtraSelections(extra_selections)

            if extra_selections:
                self.text_box.setTextCursor(extra_selections[0].cursor)
        else:
            self.text_box.setExtraSelections([])

    def open_file_location(self):
        if self.file_path is not None and self.file_path.exists():
            Directory.open_files_in_explorer(str(self.file_path.parent), [self.file_path.name])

    def regenerate_summary(self):
        if self.task_info is None:
            return

        if not config.get(config.summary_api_key):
            signal_bus.toast.show.emit(ToastNotificationCategory.WARNING, "", Translator.ERROR_MESSAGES("SUMMARY_NOT_CONFIGURED"))

            return

        if SummaryWorker.is_running_for_task(self.task_info.Basic.task_id):
            return

        self.regenerate_btn.setEnabled(False)
        self.regenerate_btn.setText(Translator.TIP_MESSAGES("GENERATING_SUMMARY"))

        # SummaryWorker 不设置 parent，生命周期由类级注册表管理
        self._summary_worker = SummaryWorker(self.task_info, update_download_status = False)
        self._summary_worker.success.connect(self.on_regenerate_success)
        self._summary_worker.error.connect(self.on_regenerate_error)

        self._summary_worker.start()

    def on_regenerate_success(self):
        self.file_path = get_summary_path(self.task_info)

        self.setWindowTitle(self.file_path.name)
        self.load_content()

        self._reset_regenerate_btn()

        signal_bus.toast.show.emit(ToastNotificationCategory.SUCCESS, "", Translator.TIP_MESSAGES("COMPLETED"))

    def on_regenerate_error(self, error_message: str):
        self._reset_regenerate_btn()

        signal_bus.toast.show_long_message.emit(
            ToastNotificationCategory.ERROR,
            Translator.ERROR_MESSAGES("SUMMARY_FAILED"),
            error_message
        )

    def _reset_regenerate_btn(self):
        self.regenerate_btn.setEnabled(True)
        self.regenerate_btn.setText(self.tr("Regenerate"))
