from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon

from qfluentwidgets import FluentIcon, MessageBox

from gui.component.dialog import TopNavigationDialogBase
from .additional import AdditionalSettingsPage
from .download import DownloadSettingsPage
from .media import MediaSettingsPage

from util.common.icon import ExtendedFluentIcon

class DownloadOptionsDialog(TopNavigationDialogBase):
    # 运行中的字幕可用性检查线程（类级持有引用，对话框销毁后线程仍可安全运行至结束）
    _subtitle_workers = []

    def __init__(self, parent = None):
        super().__init__(QSize(750, 500), parent)

        self.main_window = parent

        self.setWindowTitle(self.tr("Download Options"))
        self.setWindowIcon(QIcon(":/bili23/icon/app.svg"))

        self.setFixedSize(750, 500)

        self.init_UI()

        self.set_open_state(True)

    def init_UI(self):
        self.media_settings_page = MediaSettingsPage(self)
        self.additional_settings_page = AdditionalSettingsPage(self)
        self.download_settings_page = DownloadSettingsPage(self)

        self.addItem("media", self.tr("Media Settings"), FluentIcon.MEDIA, self.media_settings_page)
        self.addItem("additional", self.tr("Additional Files"), ExtendedFluentIcon.DOCUMENT, self.additional_settings_page)
        self.addItem("download", self.tr("Download Settings"), FluentIcon.DOWNLOAD, self.download_settings_page)

        self.pivot.setCurrentItem("media")

        self._subtitle_worker = None

        self.check_subtitle_availability()

    def check_subtitle_availability(self):
        # 后台查询所选视频是否有 B 站字幕，结果反映在附加设置页的字幕卡片上（部分视频没有字幕时提示用户）
        from util.parse.subtitle_availability import SubtitleAvailabilityWorker

        episodes = self.main_window.parse_interface.parse_list.get_checked_items(to_dict = True)

        worker = SubtitleAvailabilityWorker(episodes)
        worker.checked.connect(self.additional_settings_page.on_subtitle_availability_checked)
        worker.failed.connect(self.additional_settings_page.on_subtitle_availability_failed)
        # lambda 只引用类而非对话框实例：对话框关闭即销毁（WA_DeleteOnClose），若捕获 self，
        # 线程结束时访问已销毁的 C++ 对象会抛 RuntimeError，导致 worker 无法从注册表移除
        worker.finished.connect(lambda w = worker: DownloadOptionsDialog._on_subtitle_worker_finished(w))

        self._subtitle_worker = worker
        self._subtitle_workers.append(worker)

        # 所选视频均不支持字幕（如音频）时不发起查询
        if worker.episodes:
            worker.start()

    @classmethod
    def _on_subtitle_worker_finished(cls, worker):
        # 线程结束后移除类级引用并销毁线程对象
        # （finished 信号对非 QObject 接收者是直接连接，此槽在 worker 线程中执行；
        # 仅操作类级列表与 deleteLater，二者均可安全跨线程调用）
        if worker in cls._subtitle_workers:
            cls._subtitle_workers.remove(worker)

        worker.deleteLater()

    def _stop_subtitle_check(self):
        # 对话框关闭时请求中断仍在运行的字幕可用性检查
        try:
            if self._subtitle_worker is not None:
                self._subtitle_worker.requestInterruption()

        except RuntimeError:
            # 线程已结束并被销毁，无需中断
            pass

    def closeEvent(self, event):
        self._stop_subtitle_check()

        super().closeEvent(event)

        self.set_open_state(False)
    
    def accept(self):
        # 检查用户的设置
        if not self.media_settings_page.on_check():
            return
        
        if not self.media_settings_page.has_media_to_download() and not self.additional_settings_page.has_file_to_download():
            # 如果没有选择下载任何媒体文件，提示用户
            dialog = MessageBox(
                self.tr("No files selected for download"),
                self.tr("Please select at least one of the following: video stream, audio stream, or additional files."),
                self
            )
            dialog.hideCancelButton()
            dialog.exec()
            
            return

        self.media_settings_page.on_save()
        self.download_settings_page.on_save()

        return super().accept()

    def set_open_state(self, open: bool):
        self.main_window.parse_interface.download_options_dialog_opened = open
