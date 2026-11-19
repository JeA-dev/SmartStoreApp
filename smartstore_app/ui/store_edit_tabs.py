"""상품 수정 창의 '상세페이지'·'사진' 탭 — 스토어에 올린 상품의 상세페이지 템플릿과 사진을 바꾼다.

두 탭은 각자 '스토어에 적용' 버튼으로 바로 반영한다(기본 정보 탭의 '저장'과 따로). 적용하면 applied(원상품, 문구)를 보낸다.
"""
from __future__ import annotations

from pathlib import Path

import requests
from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from services.images import MAX_PRODUCT_IMAGES, SOURCE_EXTENSIONS
from services.store_edit import (
    DetailSource, PhotoEntry, StoreEditError, apply_detail, apply_photos, available_templates, current_photos,
    delete_user_template, load_detail_source, mappings_using_template, page_photos_changed, photos_changed,
    preview_detail, set_page_photos, template_info, validate_photos,
)
from services import detail_page
from ui.messages import describe_error
from ui.theme import set_role
from ui.workers import run_async

_THUMB = 110
# 상세페이지 사진 자리: (종류, 탭 이름, 안내). 에디토리얼 템플릿 기준 위치.
PAGE_GROUPS = (
    ("photos", "상품 사진", "첫 장은 대표 사진, 두 번째는 커버(맨 위 큰 사진)로 쓰입니다."),
    ("cuts", "착용 컷", "'이 옷의 포인트' 위에 한 장씩 크게 들어갑니다."),
    ("views", "디테일 컷", "'자세히 보기'에 두 장씩 들어갑니다. (처음엔 상품 사진 3번째부터)"),
    ("pages", "긴 상세 이미지", "공지·사이즈표까지 든 완성된 긴 이미지. 템플릿 설정에 따라 넣거나 뺍니다 (에디토리얼은 기본으로 뺌)."),
)
_FILTER = "사진 (" + " ".join(f"*{e}" for e in sorted(SOURCE_EXTENSIONS)) + ")"


def _message(exc: Exception) -> str:
    return str(exc) if isinstance(exc, StoreEditError) else describe_error(exc)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


# --- 상세페이지 ---
class DetailTab(QWidget):
    applied = Signal(object, str)

    def __init__(self, services, origin_product_no: int, raw: dict, parent=None):
        super().__init__(parent)
        self.services, self.origin_product_no, self.raw = services, origin_product_no, raw
        self.source: DetailSource | None = None
        self._loading = False

        self.info = QLabel("상세페이지 정보를 불러오려면 이 탭을 여세요.")
        self.info.setWordWrap(True)
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(220)
        self.preview_btn = QPushButton("미리보기 (브라우저)")
        self.apply_btn = set_role(QPushButton("스토어에 적용"), "primary")
        self.preview_btn.clicked.connect(self.on_preview)
        self.apply_btn.clicked.connect(self.on_apply)
        self.reload_btn = QPushButton("다시 불러오기")
        self.reload_btn.clicked.connect(self.load)
        self.delete_btn = set_role(QPushButton("템플릿 삭제"), "danger")
        self.delete_btn.setToolTip("상세템플릿 폴더에 직접 넣은 템플릿만 지울 수 있습니다 (앱 기본 템플릿은 지울 수 없음).")
        self.delete_btn.clicked.connect(self.on_delete_template)
        self.folder_btn = QPushButton("템플릿 폴더 열기")
        self.folder_btn.clicked.connect(self.on_open_folder)
        self.template_combo.currentIndexChanged.connect(lambda _: self._update_delete_button())

        template_row = QHBoxLayout()
        template_row.addWidget(self.template_combo)
        template_row.addWidget(self.delete_btn)
        template_row.addWidget(self.folder_btn)
        template_row.addStretch(1)
        form = QFormLayout()
        form.addRow("템플릿", template_row)
        # 상세페이지에 넣을 사진 (이번 적용에만 쓰고 사진 폴더는 그대로) — 자리마다 탭 하나
        self.page_lists: dict[str, PagePhotoList] = {}
        self.photo_tabs = QTabWidget()
        for kind, title, hint in PAGE_GROUPS:
            lst = PagePhotoList(kind)
            lst.model().rowsInserted.connect(self._update_photo_tab_titles)
            lst.model().rowsRemoved.connect(self._update_photo_tab_titles)
            self.page_lists[kind] = lst
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(6, 6, 6, 6)
            page_layout.addWidget(_hint(hint))
            page_layout.addWidget(lst, 1)
            self.photo_tabs.addTab(page, title)
        self.photo_add_btn = QPushButton("사진 추가...")
        self.photo_left_btn = QPushButton("◀ 앞으로")
        self.photo_right_btn = QPushButton("뒤로 ▶")
        self.photo_remove_btn = QPushButton("빼기")
        self.photo_move_btn = QPushButton("다른 탭으로 옮기기")
        self.photo_move_btn.setToolTip("고른 사진을 다른 자리(탭)로 옮깁니다.")
        self.photo_move_menu = QMenu(self.photo_move_btn)
        for kind, title, _ in PAGE_GROUPS:
            self.photo_move_menu.addAction(title, lambda kind=kind: self.move_page_photos(kind))
        self.photo_move_btn.setMenu(self.photo_move_menu)
        self.photo_reset_btn = QPushButton("원래대로")
        self.photo_add_btn.clicked.connect(self.on_add_page_photos)
        self.photo_left_btn.clicked.connect(lambda: self.active_list().move_selected(step=-1))
        self.photo_right_btn.clicked.connect(lambda: self.active_list().move_selected(step=1))
        self.photo_remove_btn.clicked.connect(lambda: self.active_list().take_selected())
        self.photo_reset_btn.clicked.connect(self.reset_page_photos)
        self._photo_widgets = (self.photo_tabs, self.photo_add_btn, self.photo_left_btn, self.photo_right_btn,
                               self.photo_remove_btn, self.photo_move_btn, self.photo_reset_btn)
        photo_tools = QHBoxLayout()
        for b in (self.photo_add_btn, self.photo_left_btn, self.photo_right_btn, self.photo_remove_btn,
                  self.photo_move_btn):
            photo_tools.addWidget(b)
        photo_tools.addStretch(1)
        photo_tools.addWidget(self.photo_reset_btn)

        buttons = QHBoxLayout()
        buttons.addWidget(self.preview_btn)
        buttons.addWidget(self.reload_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_btn)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.info)
        layout.addLayout(form)
        layout.addWidget(_hint(
            "작업 엑셀의 설명·실측·소재와 아래 사진으로 상세페이지를 다시 만들고, 상품명·판매가는 지금 스토어 값을 씁니다. "
            "'스토어에 적용'을 누르면 상세설명만 바뀌고, 바꾸기 전 상세페이지는 데이터 폴더의 '상품수정_백업'에 남습니다."))
        layout.addWidget(QLabel("<b>상세페이지에 넣을 사진</b>"))
        layout.addWidget(_hint(
            "끌어서 순서를 바꾸고, 탐색기에서 끌어다 놓거나 '사진 추가'로 넣습니다. 이번 적용에만 쓰며 사진 폴더와 "
            "스토어 상품 사진(사진 탭)은 바뀌지 않습니다."))
        layout.addLayout(photo_tools)
        layout.addWidget(self.photo_tabs, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self._set_enabled(False)

    def _set_enabled(self, ready: bool) -> None:
        for w in (self.template_combo, self.preview_btn, self.apply_btn, self.folder_btn, *self._photo_widgets):
            w.setEnabled(ready and not self._loading)
        self.reload_btn.setEnabled(not self._loading)
        self._update_delete_button()

    def current_template(self) -> str:
        return self.template_combo.currentData() or ""

    def _update_delete_button(self) -> None:
        ok = bool(self.source and not self._loading and self.template_combo.isEnabled() and self.current_template())
        self.delete_btn.setEnabled(ok and template_info(self.source.settings, self.current_template()).deletable)

    def _fill_templates(self, select: str) -> None:
        """앱 기본 템플릿은 '(앱 기본)'으로 표시한다. 항목 데이터는 템플릿 이름."""
        settings = self.source.settings
        self.source.templates = available_templates(settings)
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for name in self.source.templates:
            info = template_info(settings, name)
            label = name if info.user_file else f"{name}  (앱 기본)"
            self.template_combo.addItem(label, name)
        index = self.template_combo.findData(select)
        self.template_combo.setCurrentIndex(index if index >= 0 else max(0, self.template_combo.findData(
            detail_page.DEFAULT_TEMPLATE)))
        self.template_combo.blockSignals(False)
        self._update_delete_button()

    def ensure_loaded(self) -> None:
        if self.source is None and not self._loading:
            self.load()

    def load(self) -> None:
        self._loading = True
        self._set_enabled(False)
        self.info.setText("작업 엑셀과 사진을 확인하는 중... (사진 보정이 필요하면 조금 걸립니다)")
        db, no, raw = self.services.db, self.origin_product_no, self.raw
        run_async(lambda: load_detail_source(db, no, raw), self._on_loaded, self._on_load_failed)

    def _on_loaded(self, source: DetailSource) -> None:
        self._loading = False
        self.source = source
        self._fill_templates(source.current_template)
        self.reset_page_photos()
        images = source.prepared.images
        counts = (f"상품 사진 {1 + len(images.optional)}장 · 상세 사진 {len(images.detail)}장"
                  if images and images.found else "사진 없음")
        self.info.setText(f"상품코드 <b>{source.code}</b> · 등록 때 템플릿 <b>{source.current_template}</b> · {counts}")
        self.status.setText("\n".join(f"참고: {w}" for w in source.warnings))
        self._set_enabled(True)

    def _on_load_failed(self, exc: Exception) -> None:
        self._loading = False
        self.info.setText(_message(exc))
        self._set_enabled(False)

    # --- 상세페이지 사진 ---
    def active_list(self) -> "PagePhotoList":
        return self.page_lists[PAGE_GROUPS[max(0, self.photo_tabs.currentIndex())][0]]

    def _update_photo_tab_titles(self, *_args) -> None:
        for i, (kind, title, _) in enumerate(PAGE_GROUPS):
            self.photo_tabs.setTabText(i, f"{title} ({self.page_lists[kind].count()})")

    def reset_page_photos(self) -> None:
        if not self.source:
            return
        folder = self.source.folder_page()
        known = list(self.source.folder_files())
        for kind, _, _ in PAGE_GROUPS:
            self.page_lists[kind].set_paths(getattr(folder, kind), known)
        set_page_photos(self.source, None)
        self._update_photo_tab_titles()

    def on_add_page_photos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "상세페이지 사진 추가", "", _FILTER)
        rejected = self.active_list().add_files([Path(f) for f in files])
        if rejected:
            QMessageBox.information(self, "사진 추가", f"{len(rejected)}장은 사진 파일이 아니라 넣지 못했습니다.")

    def move_page_photos(self, kind: str) -> None:
        """고른 사진을 다른 자리(탭)로 옮긴다."""
        source_list, target = self.active_list(), self.page_lists[kind]
        if target is source_list:
            return
        for entry in source_list.take_selected():
            target.add_entry(entry)
        target.relabel()

    def page_photos(self) -> detail_page.PagePhotos:
        return detail_page.PagePhotos(*(self.page_lists[kind].paths() for kind, _, _ in PAGE_GROUPS))

    def _sync_page_photos(self) -> None:
        """화면의 사진 구성을 상세페이지 만들기에 넘긴다 (사진 폴더 기본 구성과 같으면 폴더 그대로)."""
        set_page_photos(self.source, self.page_photos())

    def on_preview(self) -> None:
        if not self.source:
            return
        self._sync_page_photos()
        try:
            path = preview_detail(self.source, self.current_template())
        except Exception as e:
            QMessageBox.warning(self, "미리보기", _message(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.status.setText(f"미리보기를 브라우저로 열었습니다: {path}")

    def on_apply(self) -> None:
        if not self.source:
            return
        template = self.current_template()
        self._sync_page_photos()
        counts = " · ".join(f"{title} {self.page_lists[kind].count()}장" for kind, title, _ in PAGE_GROUPS)
        photos_note = f"\n사진(바꾼 구성): {counts}" if page_photos_changed(self.source) else ""
        if QMessageBox.question(
                self, "상세페이지 적용",
                f"상세페이지를 '{template}' 템플릿으로 다시 만들어 스토어에 바로 반영합니다.{photos_note}\n"
                "판매중인 상품이면 고객에게 바로 보입니다. 계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self._loading = True
        self._set_enabled(False)
        self.status.setText("사진을 올리고 상세페이지를 저장하는 중...")
        services, no, source = self.services, self.origin_product_no, self.source
        run_async(lambda: apply_detail(services, no, source, template),
                  lambda raw: self._on_applied(raw, template), self._on_apply_failed)

    def on_open_folder(self) -> None:
        if not self.source:
            return
        folder = Path(detail_page.user_template_dir(self.source.settings))
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def on_delete_template(self) -> None:
        if not self.source:
            return
        name = self.current_template()
        info = template_info(self.source.settings, name)
        if not info.deletable:
            return
        lines = [f"'{name}' 템플릿을 삭제할까요?", "",
                 f"파일은 상세템플릿 폴더 안 '삭제한_템플릿' 폴더로 옮겨져 필요하면 되살릴 수 있습니다."]
        if info.builtin:
            lines.append(f"같은 이름의 앱 기본 템플릿 '{name}'이(가) 다시 쓰입니다.")
        used = mappings_using_template(self.services.db, name)
        if used:
            lines += ["", f"⚠ 이 템플릿을 쓰는 분류: {', '.join(used)}"]
            if not info.builtin:
                lines.append("이 분류의 새 상품은 자동화 등록에서 다른 템플릿을 고르기 전까지 등록되지 않습니다.")
        if name == self.source.current_template:
            lines += ["", "이미 스토어에 올라간 상세페이지는 그대로입니다."]
        if QMessageBox.question(self, "템플릿 삭제", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        try:
            moved = delete_user_template(self.source.settings, name)
        except Exception as e:
            QMessageBox.warning(self, "템플릿 삭제", _message(e))
            return
        self._fill_templates(name if info.builtin else self.source.current_template)
        self.status.setText(f"'{name}' 템플릿을 삭제했습니다 (옮긴 곳: {moved})")

    def _on_applied(self, raw: dict, template: str) -> None:
        self._loading = False
        self.raw = raw
        self._set_enabled(True)
        message = f"상세페이지 → {template}"
        self.status.setText(f"적용 완료: {message}")
        self.applied.emit(raw, message)

    def _on_apply_failed(self, exc: Exception) -> None:
        self._loading = False
        self._set_enabled(True)
        self.status.setText("적용 실패")
        QMessageBox.critical(self, "상세페이지 적용 실패", _message(exc))


# --- 사진 ---
def fetch_images(urls: list[str]) -> dict[str, bytes]:
    """스토어 사진을 미리보기용으로 내려받는다. 실패한 사진은 빠진다."""
    result = {}
    for url in urls:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            result[url] = r.content
        except requests.RequestException:
            continue
    return result


class StorePhotoList(QListWidget):
    """스토어 사진(주소)과 새 사진(파일)을 함께 담는 목록. 끌어서 순서 바꾸기, 탐색기에서 끌어다 놓기."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # IconMode는 끌어 놓으면 위치만 옮기고 순서는 그대로라, ListMode를 가로로 흐르게 해서 순서가 바뀌게 한다
        self.setViewMode(QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setIconSize(QSize(_THUMB, _THUMB))
        self.setGridSize(QSize(_THUMB + 96, _THUMB + 14))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)
        self.setMinimumHeight(2 * (_THUMB + 14) + 12)
        self.model().rowsMoved.connect(self.relabel)
        self.model().rowsInserted.connect(self.relabel)
        self.model().rowsRemoved.connect(self.relabel)

    def entries(self) -> list[PhotoEntry]:
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]

    def add_entry(self, entry: PhotoEntry, pixmap: QPixmap | None = None) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setToolTip(entry.path or entry.url)
        if pixmap is None and entry.is_new:
            pixmap = QPixmap(entry.path)
        if pixmap is not None and not pixmap.isNull():
            self._set_icon(item, pixmap)
        self.addItem(item)

    @staticmethod
    def _set_icon(item: QListWidgetItem, pixmap: QPixmap) -> None:
        thumb = pixmap.scaled(_THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        icon = QIcon(thumb)
        icon.addPixmap(thumb, QIcon.Mode.Selected)
        item.setIcon(icon)

    def set_url_images(self, data: dict[str, bytes]) -> None:
        for i in range(self.count()):
            item = self.item(i)
            entry: PhotoEntry = item.data(Qt.ItemDataRole.UserRole)
            if entry.url in data:
                pixmap = QPixmap()
                if pixmap.loadFromData(data[entry.url]):
                    self._set_icon(item, pixmap)

    def add_files(self, paths: list[Path]) -> list[Path]:
        """사진 파일만 추가한다. 10장을 넘거나 사진이 아니면 돌려준다."""
        rejected = []
        for path in paths:
            if path.suffix.lower() not in SOURCE_EXTENSIONS or not path.is_file() or \
                    self.count() >= MAX_PRODUCT_IMAGES:
                rejected.append(path)
                continue
            self.add_entry(PhotoEntry(path=str(path)))
        return rejected

    def relabel(self, *_args) -> None:
        for i in range(self.count()):
            entry: PhotoEntry = self.item(i).data(Qt.ItemDataRole.UserRole)
            label = "대표" if i == 0 else f"추가 {i}"
            self.item(i).setText(label + (" · 새 사진" if entry and entry.is_new else ""))

    def move_selected(self, target: int | None = None, step: int = 0) -> None:
        row = self.currentRow()
        if row < 0:
            return
        new_row = target if target is not None else row + step
        if not 0 <= new_row < self.count() or new_row == row:
            return
        item = self.takeItem(row)
        self.insertItem(new_row, item)
        self.setCurrentRow(new_row)

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            self.add_files([Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()])
            event.acceptProposedAction()
        elif event.source() is self:
            super().dropEvent(event)
        else:
            event.ignore()


class PhotoTab(QWidget):
    applied = Signal(object, str)

    def __init__(self, services, origin_product_no: int, raw: dict, parent=None):
        super().__init__(parent)
        self.services, self.origin_product_no, self.raw = services, origin_product_no, raw
        self._images: dict[str, bytes] = {}
        self._loaded = False
        self._busy = False

        self.list = StorePhotoList()
        self.add_btn = QPushButton("사진 추가...")
        self.main_btn = QPushButton("대표 사진으로")
        self.left_btn = QPushButton("◀ 앞으로")
        self.right_btn = QPushButton("뒤로 ▶")
        self.remove_btn = QPushButton("빼기")
        self.reset_btn = QPushButton("원래대로")
        self.apply_btn = set_role(QPushButton("스토어에 적용"), "primary")
        self.add_btn.clicked.connect(self.on_add)
        self.main_btn.clicked.connect(lambda: self.list.move_selected(target=0))
        self.left_btn.clicked.connect(lambda: self.list.move_selected(step=-1))
        self.right_btn.clicked.connect(lambda: self.list.move_selected(step=1))
        self.remove_btn.clicked.connect(self.list.remove_selected)
        self.reset_btn.clicked.connect(self.reset)
        self.apply_btn.clicked.connect(self.on_apply)

        tools = QHBoxLayout()
        for b in (self.add_btn, self.main_btn, self.left_btn, self.right_btn, self.remove_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.reset_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.apply_btn)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(
            f"첫 번째 사진이 대표 사진이고, 최대 {MAX_PRODUCT_IMAGES}장(대표 1 + 추가 {MAX_PRODUCT_IMAGES - 1})까지 넣을 수 있습니다. "
            "끌어서 순서를 바꾸고, 탐색기에서 사진을 끌어다 놓아 추가할 수 있습니다. "
            "새 사진은 정사각형(1000×1000)으로 맞춰 올리며 원본 파일은 그대로 둡니다. "
            "상세페이지 안의 사진은 바뀌지 않습니다(상세페이지 탭에서 다시 만들기)."))
        layout.addLayout(tools)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addLayout(bottom)

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.reset()

    def reset(self) -> None:
        self.list.clear()
        entries = current_photos(self.raw)
        for e in entries:
            self.list.add_entry(e)
        self.list.relabel()
        missing = [e.url for e in entries if e.url not in self._images]
        if missing:
            self.status.setText("스토어 사진을 불러오는 중...")
            run_async(lambda: fetch_images(missing), self._on_images, lambda e: self.status.setText(_message(e)))
        else:
            self.list.set_url_images(self._images)
            self.status.setText("")

    def _on_images(self, data: dict[str, bytes]) -> None:
        self._images.update(data)
        self.list.set_url_images(self._images)
        if not data:
            self.status.setText("사진 미리보기를 불러오지 못했습니다. (적용에는 영향 없음)")
        elif self.status.text().startswith("스토어 사진을 불러오는"):
            self.status.setText("")

    def on_add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "사진 추가", "", _FILTER)
        rejected = self.list.add_files([Path(f) for f in files])
        self.list.relabel()
        if rejected:
            QMessageBox.information(self, "사진 추가", f"{len(rejected)}장은 넣지 못했습니다. "
                                    f"(최대 {MAX_PRODUCT_IMAGES}장, 사진 파일만)")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.add_btn, self.main_btn, self.left_btn, self.right_btn, self.remove_btn, self.reset_btn,
                  self.apply_btn):
            b.setEnabled(not busy)
        self.list.setEnabled(not busy)

    def on_apply(self) -> None:
        entries = self.list.entries()
        errors = validate_photos(entries)
        if errors:
            QMessageBox.warning(self, "사진 확인", "\n".join(f"• {e}" for e in errors))
            return
        if not photos_changed(self.raw, entries):
            QMessageBox.information(self, "사진", "바뀐 사진이 없습니다.")
            return
        new = sum(e.is_new for e in entries)
        if QMessageBox.question(
                self, "사진 적용",
                f"대표 1장 + 추가 {len(entries) - 1}장으로 스토어 상품 사진을 바꿉니다"
                + (f" (새 사진 {new}장 업로드)" if new else "") + ".\n"
                "판매중인 상품이면 고객에게 바로 보입니다. 계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self.status.setText("사진을 맞추고 올리는 중...")
        services, no = self.services, self.origin_product_no
        run_async(lambda: apply_photos(services, no, entries), self._on_applied, self._on_failed)

    def _on_applied(self, result: tuple[dict, str]) -> None:
        raw, message = result
        self.raw = raw
        self._set_busy(False)
        self.reset()
        self.status.setText(f"적용 완료: {message}")
        self.applied.emit(raw, message)

    def _on_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status.setText("적용 실패")
        QMessageBox.critical(self, "사진 적용 실패", _message(exc))


# --- 상세페이지에 넣을 사진 ---
class PagePhotoList(StorePhotoList):
    """상세페이지에 넣을 PC 사진 목록 (kind: PAGE_GROUPS의 종류).

    사진 폴더에 없는 사진은 '새 사진'으로 표시한다. 장수 제한은 없다 (스토어 상품 사진과 별개).
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.known: set[str] = set()
        self.setMinimumHeight(_THUMB + 34)
        self.setGridSize(QSize(_THUMB + 70, _THUMB + 14))

    def set_paths(self, paths: list[Path], known: list[Path]) -> None:
        self.clear()
        self.known = {str(p) for p in known}
        for path in paths:
            self.add_entry(PhotoEntry(path=str(path)))
        self.relabel()

    def paths(self) -> list[Path]:
        return [Path(e.path) for e in self.entries()]

    def add_files(self, paths: list[Path]) -> list[Path]:
        rejected = []
        for path in paths:
            if path.suffix.lower() not in SOURCE_EXTENSIONS or not path.is_file():
                rejected.append(path)
                continue
            self.add_entry(PhotoEntry(path=str(path)))
        self.relabel()
        return rejected

    def take_selected(self) -> list[PhotoEntry]:
        entries = []
        for item in sorted(self.selectedItems(), key=self.row):
            entries.append(item.data(Qt.ItemDataRole.UserRole))
            self.takeItem(self.row(item))
        self.relabel()
        return entries

    def relabel(self, *_args) -> None:
        for i in range(self.count()):
            entry: PhotoEntry = self.item(i).data(Qt.ItemDataRole.UserRole)
            if not entry:
                continue
            if self.kind == "photos":
                label = ("대표", "커버")[i] if i < 2 else f"사진 {i + 1}"
            else:
                label = f"{_LIST_LABELS[self.kind]} {i + 1}"
            self.item(i).setText(label + ("" if entry.path in self.known else " · 새 사진"))


_LIST_LABELS = {"cuts": "착용", "views": "디테일", "pages": "긴 이미지"}
