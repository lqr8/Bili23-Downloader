from PySide6.QtCore import QSize, QModelIndex, Qt, QRect, QEvent, QObject
from PySide6.QtWidgets import QStyleOptionViewItem
from PySide6.QtGui import QPainter, QMouseEvent

from qfluentwidgets import FluentIcon, Action, RoundMenu

from gui.component.view_model import CoverQueryDelegateBase

from util.common.icon import ExtendedFluentIcon
from util.common.io.directory import Directory
from util.common.translator import Translator
from util.download.task.info import TaskInfo
from util.summary.worker import get_transcript_path, get_summary_path
from util.common.enum import DownloadStatus, TranscriptSource
from util.format.units import Units
from util.format.time import Time

from pathlib import Path

class DownloadItemDelegate(CoverQueryDelegateBase):
    def __init__(self, parent = None):
        super().__init__(parent)

        self.uiRect = UIRect()
        self.uiData = UIData(self)

        self.ActionButtonHoveredRow = -1
        self.DeleteButtonHoveredRow = -1
        self.TranscriptButtonHoveredRow = -1
        self.SummaryButtonHoveredRow = -1

        # 任务的字幕/总结文件查询结果缓存（task_id → (transcript_path, summary_exists)）。
        # 任务进入已完成状态时这些文件已全部写出，此后结果不变；缓存可避免每次重绘/悬停都扫描下载目录
        self._additional_file_cache = {}

    def sizeHint(self, option, index):
        return QSize(0, 100)

    def editorEvent(self, event: QEvent, model, option, index: QModelIndex):
        view = self.parent()

        if event.type() == QEvent.Type.MouseMove:
            self._buttonHoverEvent(option, index, event)
            view.update(index)

        if event.type() == QEvent.Type.Leave:
            self.hoverRow = -1
            view.update()

        if event.type() == QEvent.Type.MouseButtonRelease:
            return self._pressEvent(option, index, event)

        return super().editorEvent(event, model, option, index)

    def _paintItemUI(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        # 获取任务信息
        task_info: TaskInfo = index.data(Qt.ItemDataRole.UserRole)

        # 左侧封面、标题和信息
        coverRect = self.uiRect.getCoverRect(option)
        self._drawCover(painter, coverRect, option, index, task_info.Basic.cover_id, task_info.Episode.cover)

        titleRect = self.uiRect.getTitleRect(coverRect, option)
        self._drawText(painter, titleRect, task_info.Basic.show_title)

        infoRect = self.uiRect.getInfoRect(titleRect, option, completed = self.isTaskCompleted(task_info))
        self._drawDescriptionText(painter, infoRect, self.uiData.getInfoText(task_info))

        # 右侧进度条、状态（已完成任务右侧多出查看字幕/总结按钮，进度条相应左移让出空间）
        progressBarRect = self.uiRect.getProgressBarRect(titleRect, option, extra_button_count = 2 if self.isTaskCompleted(task_info) else 0)

        statusRect = self.uiRect.getStatusRect(infoRect, option)
        sizeRect = self.uiRect.getSizeRect(infoRect, statusRect)

        self._drawDescriptionText(painter, sizeRect, self.uiData.getSizeText(task_info))
        self._drawProgressBar(painter, progressBarRect, task_info.Download.progress, error = self.isTaskFailed(task_info), paused = self.isTaskPaused(task_info))
        self._drawDescriptionText(painter, statusRect, self.uiData.getStatusText(task_info), error = self.isTaskFailed(task_info))


        # 右侧控制和删除按钮
        actionButtonRect = self.uiRect.getActionButtonRect(option)
        self._drawPrimaryButton(painter, actionButtonRect, self.uiData.getButtonIcon(task_info), self.ActionButtonHoveredRow == index.row())

        deleteButtonRect = self.uiRect.getDeleteButtonRect(option)
        self._drawButton(painter, deleteButtonRect, FluentIcon.DELETE, self.DeleteButtonHoveredRow == index.row())

        # 已完成任务的查看字幕 / 查看总结按钮（位于控制按钮左侧）
        if self.isTaskCompleted(task_info):
            if self.hasTranscriptFile(task_info):
                transcriptButtonRect = self.uiRect.getTranscriptButtonRect(option)
                self._drawButton(painter, transcriptButtonRect, ExtendedFluentIcon.SUBTITLES, self.TranscriptButtonHoveredRow == index.row())

            if self.hasSummaryEntry(task_info):
                summaryButtonRect = self.uiRect.getSummaryButtonRect(option)
                self._drawButton(painter, summaryButtonRect, FluentIcon.ROBOT, self.SummaryButtonHoveredRow == index.row())
    
    def _buttonHoverEvent(self, option: QStyleOptionViewItem, index: QModelIndex, event: QMouseEvent):
        pos = event.pos()

        actionButtonRect = self.uiRect.getActionButtonRect(option)
        deleteButtonRect = self.uiRect.getDeleteButtonRect(option)

        if actionButtonRect.contains(pos):
            self.ActionButtonHoveredRow = index.row()
        else:
            self.ActionButtonHoveredRow = -1

        if deleteButtonRect.contains(pos):
            self.DeleteButtonHoveredRow = index.row()
        else:
            self.DeleteButtonHoveredRow = -1

        task_info: TaskInfo = index.data(Qt.ItemDataRole.UserRole)

        if task_info and self.isTaskCompleted(task_info) and self.hasTranscriptFile(task_info) and self.uiRect.getTranscriptButtonRect(option).contains(pos):
            self.TranscriptButtonHoveredRow = index.row()
        else:
            self.TranscriptButtonHoveredRow = -1

        if task_info and self.isTaskCompleted(task_info) and self.hasSummaryEntry(task_info) and self.uiRect.getSummaryButtonRect(option).contains(pos):
            self.SummaryButtonHoveredRow = index.row()
        else:
            self.SummaryButtonHoveredRow = -1

    def _pressEvent(self, option: QStyleOptionViewItem, index: QModelIndex, event: QMouseEvent):
        pos = event.pos()

        if event.button() == Qt.MouseButton.RightButton:
            # 右键点击，弹出上下文菜单
            self.contextMenuRequested.emit(index, event.globalPos())

            return True

        actionButtonRect = self.uiRect.getActionButtonRect(option)
        deleteButtonRect = self.uiRect.getDeleteButtonRect(option)

        if actionButtonRect.contains(pos):
            task_info: TaskInfo = index.data(Qt.ItemDataRole.UserRole)

            match task_info.Download.status:
                case DownloadStatus.COMPLETED:
                    self.openFileLocation(task_info)

                case _:
                    index.model().togglePauseResume(task_info)

            index.model().dataChanged.emit(index, index)

            return True

        if deleteButtonRect.contains(pos):
            index.model().cancelDownload(index.data(Qt.ItemDataRole.UserRole))

            return True

        task_info: TaskInfo = index.data(Qt.ItemDataRole.UserRole)

        if task_info and self.isTaskCompleted(task_info):
            if self.hasTranscriptFile(task_info) and self.uiRect.getTranscriptButtonRect(option).contains(pos):
                self.openTranscriptViewer(task_info, event)

                return True

            if self.hasSummaryEntry(task_info) and self.uiRect.getSummaryButtonRect(option).contains(pos):
                summary_path = get_summary_path(task_info)

                self.openTextViewer(summary_path if summary_path.exists() else None, task_info, is_summary = True)

                return True

        return False

    def openTranscriptViewer(self, task_info: TaskInfo, event: QMouseEvent):
        # ASR 字幕与 B 站字幕都存在时弹出菜单选择，否则直接打开唯一可用的字幕
        asr_path = get_transcript_path(task_info, TranscriptSource.ASR)
        cc_path = get_transcript_path(task_info, TranscriptSource.CC)

        if asr_path is not None and cc_path is not None:
            menu = RoundMenu(parent = self.parent().window())

            menu.addAction(Action(ExtendedFluentIcon.SUBTITLES, Translator.TRANSCRIPT_SOURCE("CC"), triggered = lambda: self.openTextViewer(cc_path, task_info, is_summary = False)))
            menu.addAction(Action(FluentIcon.MICROPHONE, Translator.TRANSCRIPT_SOURCE("ASR"), triggered = lambda: self.openTextViewer(asr_path, task_info, is_summary = False)))

            menu.exec(event.globalPos())
        else:
            self.openTextViewer(asr_path or cc_path, task_info, is_summary = False)

    def openTextViewer(self, file_path: Path | None, task_info: TaskInfo, is_summary: bool):
        from gui.dialog.viewer import TextViewerDialog

        dialog = TextViewerDialog(file_path, task_info, is_summary = is_summary, parent = self.parent().window())
        dialog.show()

    def hasTranscriptFile(self, task_info: TaskInfo):
        return self._get_additional_file_info(task_info)[0] is not None

    def hasSummaryEntry(self, task_info: TaskInfo):
        # 已有总结文件，或已有转写文本（可手动生成总结）时，显示查看总结按钮
        transcript_path, summary_exists = self._get_additional_file_info(task_info)

        return summary_exists or transcript_path is not None

    def _get_additional_file_info(self, task_info: TaskInfo):
        task_id = task_info.Basic.task_id

        if task_id not in self._additional_file_cache:
            transcript_path = get_transcript_path(task_info)
            summary_exists = get_summary_path(task_info).exists()

            self._additional_file_cache[task_id] = (transcript_path, summary_exists)

        return self._additional_file_cache[task_id]

    def openFileLocation(self, task_info: TaskInfo):
        directory = Path(task_info.File.download_path, task_info.File.folder)

        Directory.open_files_in_explorer(str(directory), task_info.File.relative_files)

    def isTaskCompleted(self, task_info: TaskInfo):
        return task_info.Download.status == DownloadStatus.COMPLETED
    
    def isTaskFailed(self, task_info: TaskInfo):
        return task_info.Download.status in [DownloadStatus.FAILED, DownloadStatus.FFMPEG_FAILED]
    
    def isTaskPaused(self, task_info: TaskInfo):
        return task_info.Download.status == DownloadStatus.PAUSED

class UIRect:
    def __init__(self):
        self.margin = 10
        self.spacer = self.margin * 2
        self.buttonSize = 32

    def getCoverRect(self, option: QStyleOptionViewItem):
        top = self.margin + option.rect.top()

        return QRect(self.margin, top, 144, 80)

    def getTitleRect(self, coverRect: QRect, option: QStyleOptionViewItem):
        left = coverRect.right() + self.spacer
        top = coverRect.top() + 5

        width = option.rect.width() - 450

        return QRect(left, top, width, 20)
    
    def getInfoRect(self, titleRect: QRect, option: QStyleOptionViewItem, completed = False):
        left = titleRect.left()
        top = option.rect.bottom() - titleRect.height() - self.margin - 5

        if completed:
            width = 175
        else:
            width = 125
        
        return QRect(left, top, width, 20)
    
    def getSizeRect(self, infoRect: QRect, statusRect: QRect):
        # 文件大小位于信息与状态之间，两者空间不足时压缩宽度（文字以省略号截断），避免与状态文字重叠
        left = infoRect.right() + self.margin
        width = max(0, min(150, statusRect.left() - self.margin - left))

        top = infoRect.top()

        return QRect(left, top, width, 20)

    def getProgressBarRect(self, titleRect: QRect, option: QStyleOptionViewItem, extra_button_count: int = 0):
        left = option.rect.width() - self.margin - self.buttonSize * 2 - self.spacer * 3 - 200 - extra_button_count * (self.buttonSize + self.margin)
        top = (option.rect.height() - 16) / 2 + option.rect.top()  #titleRect.top() + self.margin

        return QRect(left, top, 200, 16)
    
    def getStatusRect(self, infoRect: QRect, option: QStyleOptionViewItem):
        # 状态文字固定显示在底部信息行右侧，与进度条位置无关
        # （已完成任务进度条会为查看字幕/总结按钮左移，若状态跟随左移会与文件大小文字重叠）
        left = option.rect.width() - self.margin - self.buttonSize * 2 - self.spacer * 3 - 200
        top = infoRect.top()

        return QRect(left, top, 200, 20)

    def getActionButtonRect(self, option: QStyleOptionViewItem):
        left = option.rect.width() - self.buttonSize * 2 - self.spacer * 2
        top = (option.rect.height() - self.buttonSize) / 2 + option.rect.top()

        return QRect(left, top, self.buttonSize, self.buttonSize)
    
    def getDeleteButtonRect(self, option: QStyleOptionViewItem):
        left = option.rect.width() - self.buttonSize - self.spacer * 2 + self.margin
        top = (option.rect.height() - self.buttonSize) / 2 + option.rect.top()

        return QRect(left, top, self.buttonSize, self.buttonSize)

    def getSummaryButtonRect(self, option: QStyleOptionViewItem):
        # 查看总结按钮，位于控制按钮左侧
        actionButtonRect = self.getActionButtonRect(option)
        left = actionButtonRect.left() - self.margin - self.buttonSize
        top = actionButtonRect.top()

        return QRect(left, top, self.buttonSize, self.buttonSize)

    def getTranscriptButtonRect(self, option: QStyleOptionViewItem):
        # 查看字幕按钮，位于查看总结按钮左侧
        summaryButtonRect = self.getSummaryButtonRect(option)
        left = summaryButtonRect.left() - self.margin - self.buttonSize
        top = summaryButtonRect.top()

        return QRect(left, top, self.buttonSize, self.buttonSize)

class UIData(QObject):
    def __init__(self, parent = None):
        super().__init__(parent)

    def getInfoText(self, task_info: TaskInfo):
        if task_info.Download.status == DownloadStatus.COMPLETED:
            return Time.format_timestamp(task_info.Basic.completed_time)
        else:
            return task_info.Download.info_label

    def getStatusText(self, task_info: TaskInfo):
        match task_info.Download.status:
            case DownloadStatus.QUEUED:
                return Translator.TIP_MESSAGES("QUEUED")
            
            case DownloadStatus.PARSING:
                return Translator.TIP_MESSAGES("PARSING")
            
            case DownloadStatus.DOWNLOADING:
                return self.getSpeedText(task_info)
            
            case DownloadStatus.PAUSED:
                return Translator.TIP_MESSAGES("PAUSED")
            
            case DownloadStatus.FFMPEG_QUEUED:
                return Translator.TIP_MESSAGES("FFMPEG_QUEUED")
            
            case DownloadStatus.MERGING:
                return Translator.TIP_MESSAGES("MERGING")
            
            case DownloadStatus.ADDITIONAL_PROCESSING:
                return task_info.Download.status_label
            
            case DownloadStatus.CONVERTING:
                return Translator.TIP_MESSAGES("CONVERTING")
            
            case DownloadStatus.COMPLETED:
                return Translator.TIP_MESSAGES("COMPLETED")
            
            case DownloadStatus.FAILED:
                return Translator.ERROR_MESSAGES("DOWNLOAD_FAILED")
            
            case DownloadStatus.FFMPEG_FAILED:
                return Translator.ERROR_MESSAGES("FFMPEG_PROCESSING_FAILED")
            
    def getSpeedText(self, task_info: TaskInfo):
        return Units.format_speed(task_info.Download.speed)
    
    def getSizeText(self, task_info: TaskInfo):
        if task_info.Download.total_size > 0:

            if task_info.Download.status in [DownloadStatus.COMPLETED, DownloadStatus.FFMPEG_QUEUED, DownloadStatus.MERGING, DownloadStatus.CONVERTING, DownloadStatus.FFMPEG_FAILED]:
                return Units.format_file_size(task_info.Download.total_size)
            else:
                return f"{Units.format_file_size(task_info.Download.downloaded_size)} / {Units.format_file_size(task_info.Download.total_size)}"
            
        else:
            return ""
        
    def getButtonIcon(self, task_info: TaskInfo):
        match task_info.Download.status:
            case DownloadStatus.COMPLETED:
                return FluentIcon.FOLDER
            
            case DownloadStatus.QUEUED | DownloadStatus.PAUSED | DownloadStatus.FFMPEG_QUEUED:
                return FluentIcon.PLAY
            
            case DownloadStatus.FAILED | DownloadStatus.FFMPEG_FAILED:
                return ExtendedFluentIcon.RETRY
            
            case _:
                return FluentIcon.PAUSE
            