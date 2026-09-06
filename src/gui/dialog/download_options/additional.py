from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtCore import QTimer

from gui.component.setting import DanmakuSettingCard, SubtitleSettingCard, CoverSettingCard, MetadataSettingCard, ASRSettingCard, SummarySettingCard
from gui.component.widget import ScrollArea

from util.common.config import config
from util.common.enum import TranscriptSource

class AdditionalSettingsPage(ScrollArea):
    def __init__(self, parent = None):
        super().__init__(parent)

        self.init_UI()

        QTimer.singleShot(0, self.expand_all)

    def init_UI(self):
        self.danmaku_card = DanmakuSettingCard(full_mode = False, parent = self)
        self.subtitle_card = SubtitleSettingCard(full_mode = False, parent = self)
        self.cover_card = CoverSettingCard(parent = self)
        self.metadata_card = MetadataSettingCard(parent = self)
        self.asr_card = ASRSettingCard(full_mode = False, parent = self)
        self.summary_card = SummarySettingCard(full_mode = False, parent = self)

        # AI 总结依赖语音转文字或 B 站字幕，二者均未开启时总结开关不可用；
        # 只开启其中一种字幕来源时，总结来源自动固定为可用的那一种
        self.asr_card.asr_switch.checkedChanged.connect(self.on_source_toggled)
        self.subtitle_card.download_switch.checkedChanged.connect(self.on_source_toggled)
        self.on_source_toggled()

        main_layout = QVBoxLayout()
        main_layout.addWidget(self.danmaku_card)
        main_layout.addWidget(self.subtitle_card)
        main_layout.addWidget(self.cover_card)
        main_layout.addWidget(self.metadata_card)
        main_layout.addWidget(self.asr_card)
        main_layout.addWidget(self.summary_card)
        main_layout.addStretch()

        self.setScrollLayout(main_layout)

    def expand_all(self):
        self.danmaku_card.toggleExpand()
        self.subtitle_card.toggleExpand()
        self.cover_card.toggleExpand()
        self.metadata_card.toggleExpand()
        self.asr_card.toggleExpand()
        self.summary_card.toggleExpand()

    def on_source_toggled(self):
        asr_checked = self.asr_card.asr_switch.isChecked()
        subtitle_checked = self.subtitle_card.download_switch.isChecked()

        # 字幕下载开关开启但所选视频都没有 B 站字幕时，B 站字幕来源视为不可用
        cc_usable = subtitle_checked and self.subtitle_card.has_available_subtitles()

        self.summary_card.summary_switch.setEnabled(asr_checked or cc_usable)

        source_choice = self.summary_card.source_choice

        if asr_checked != cc_usable:
            # 仅一种字幕来源可用时固定为该来源，无需选择
            source_choice.setEnabled(False)

            self._set_source_display(TranscriptSource.ASR if asr_checked else TranscriptSource.CC)
        else:
            # 两种来源都可用（或都未开启）时由用户自行选择，恢复为用户保存的来源
            source_choice.setEnabled(True)

            self._set_source_display(config.get(config.summary_transcript_source))

    def _set_source_display(self, source: TranscriptSource):
        # 阻断信号：联动只调整界面显示，不触发 currentIndexChanged 改写用户保存的来源配置
        source_choice = self.summary_card.source_choice

        source_choice.blockSignals(True)
        source_choice.setCurrentIndex(config.summary_transcript_source.options.index(source))
        source_choice.blockSignals(False)

    def on_subtitle_availability_checked(self, no_count: int, ai_count: int, uploader_count: int):
        self.subtitle_card.set_subtitle_availability(no_count, ai_count, uploader_count)

        # 可用性结果影响总结来源的自动默认，重新联动一次
        self.on_source_toggled()

    def on_subtitle_availability_failed(self):
        self.subtitle_card.set_subtitle_check_failed()

        self.on_source_toggled()

    def has_file_to_download(self):
        return (
            self.danmaku_card.download_switch.isChecked() or
            self.subtitle_card.download_switch.isChecked() or
            self.cover_card.download_switch.isChecked() or
            self.metadata_card.download_switch.isChecked()
        )
