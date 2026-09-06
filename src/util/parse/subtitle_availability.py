from PySide6.QtCore import QThread, Signal

from ..network.request import SyncNetWorkRequest
from .parser.base import ParserBase

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

logger = logging.getLogger(__name__)

# 单个视频的字幕来源分类：无字幕 / 仅 B 站 AI 自动生成字幕 / 有 UP 主上传的字幕
SUBTITLE_NONE = "none"
SUBTITLE_AI_ONLY = "ai"
SUBTITLE_UPLOADER = "uploader"

class _SubtitleQuery(ParserBase):
    """按 bvid + cid 查询单个视频在 B 站的字幕来源。"""

    def classify(self, bvid: str, cid: int) -> str:
        params = {
            "bvid": bvid,
            "cid": cid,
            "dm_img_list": "[]",
            "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9tZXVtKQ",
            "dm_cover_img_str": "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgUlRYIDQwNjAgTGFwdG9wIEdQVSAoMHgwMDAwMjhFMCkgRGlyZWN0M0QxMSlHb29nbGUgSW5jLiAoTlZJRElBKQ",
            "dm_img_inter": '{"ds":[],"wh":[5231,6067,75],"of":[475,950,475]}',
        }

        url = f"https://api.bilibili.com/x/player/wbi/v2?{self.enc_wbi(params)}"

        request = SyncNetWorkRequest(url)
        response = request.run()

        self.check_response(response)

        subtitles = response["data"]["subtitle"]["subtitles"]

        if not subtitles:
            return SUBTITLE_NONE

        # AI 自动生成字幕的语言代码以 ai 开头（如 ai-zh），其余为 UP 主上传的字幕；
        # 两种同时存在时视为有 UP 主字幕
        if all(entry.get("lan", "").startswith("ai") for entry in subtitles):
            return SUBTITLE_AI_ONLY

        return SUBTITLE_UPLOADER

class SubtitleAvailabilityWorker(QThread):
    """批量查询所选视频的字幕可用性与来源（UP 主字幕 / 仅 AI 字幕），供下载设置对话框提示用户。

    下载选项对话框打开时在后台启动；任一查询失败即视为本次检查失败（部分结果会误导用户），
    对话框关闭时通过 requestInterruption() 停止剩余查询。
    """
    checked = Signal(int, int, int)  # (无字幕数, 仅 AI 字幕数, 有 UP 主字幕数)
    failed = Signal()

    MAX_WORKERS = 4

    def __init__(self, episodes: list, parent = None):
        super().__init__(parent)

        # 音频等类型没有 bvid/cid，不参与字幕可用性检查
        self.episodes = [episode for episode in episodes if episode.get("bvid") and episode.get("cid")]

    def run(self):
        total = len(self.episodes)

        if total == 0:
            self.checked.emit(0, 0, 0)

            return

        query = _SubtitleQuery()
        counts = {SUBTITLE_NONE: 0, SUBTITLE_AI_ONLY: 0, SUBTITLE_UPLOADER: 0}
        error = None

        executor = ThreadPoolExecutor(max_workers = self.MAX_WORKERS)

        try:
            futures = [executor.submit(query.classify, episode["bvid"], episode["cid"]) for episode in self.episodes]

            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    return

                try:
                    counts[future.result()] += 1

                except Exception as e:
                    error = e

                    break

        finally:
            executor.shutdown(wait = False, cancel_futures = True)

        if error is not None:
            logger.warning(f"字幕可用性检查失败：{error}")

            self.failed.emit()

        else:
            self.checked.emit(counts[SUBTITLE_NONE], counts[SUBTITLE_AI_ONLY], counts[SUBTITLE_UPLOADER])
