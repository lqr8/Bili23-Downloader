from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtCore import Qt

from qfluentwidgets import SubtitleLabel, BodyLabel, PasswordLineEdit, EditableComboBox, HyperlinkLabel

from gui.component.dialog import DialogBase

from util.common.config import config

import logging

logger = logging.getLogger(__name__)

class ASRSettingsDialog(DialogBase):
    def __init__(self, parent = None):
        super().__init__(parent)

        self.init_UI()

    def init_UI(self):
        self.caption_lab = SubtitleLabel(self.tr("Configure Speech-to-Text Service"), self)

        self.hyper_label = HyperlinkLabel(self)
        self.hyper_label.setUrl("https://bailian.console.aliyun.com/?apiKey=1")
        self.hyper_label.setText(self.tr("Get Alibaba Cloud Bailian API Key"))

        api_key_lab = BodyLabel(self.tr("API Key"))
        self.api_key_box = PasswordLineEdit(self)
        self.api_key_box.setPlaceholderText(self.tr("DashScope API Key, e.g. sk-xxxxxxxxxxxxxxxx"))
        self.api_key_box.setText(config.get(config.asr_api_key))

        model_lab = BodyLabel(self.tr("Model Name"))
        self.model_box = EditableComboBox(self)
        self.model_box.addItems([
            "qwen3-asr-flash-filetrans",
            "qwen-audio-3.0-asr-flash-filetrans",
            "fun-asr",
            "paraformer-v2"
        ])
        self.model_box.setCurrentText(config.get(config.asr_model))
        self.model_box.setFixedWidth(360)

        tip_lab = BodyLabel(self.tr("The audio file will be uploaded to the temporary storage space of Alibaba Cloud Bailian for transcription (retained for 48 hours)."))
        tip_lab.setWordWrap(True)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.caption_lab)
        layout.setSpacing(10)
        layout.addWidget(self.hyper_label, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(api_key_lab)
        layout.addWidget(self.api_key_box)
        layout.addWidget(model_lab)
        layout.addWidget(self.model_box, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(5)
        layout.addWidget(tip_lab)

        self.viewLayout.addLayout(layout)

        self.widget.setMinimumWidth(520)

    def accept(self):
        config.set(config.asr_api_key, self.api_key_box.text().strip())
        config.set(config.asr_model, self.model_box.currentText().strip())

        return super().accept()
