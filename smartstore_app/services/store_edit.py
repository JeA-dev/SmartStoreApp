"""스토어에 올린 상품의 상세페이지 템플릿 바꾸기 · 사진 바꾸기 (상품 수정 창의 '상세페이지'·'사진' 탭).

둘 다 원상품을 새로 조회해 그 부분(detailContent / images)만 바꿔 다시 보낸다. 다른 정보는 스토어에 있는 그대로 둔다.
바꾸기 전 내용은 데이터 폴더의 '상품수정_백업'에 남긴다. 원본 사진은 읽기만 하고, 새로 고른 사진의 보정본은
데이터 폴더의 '사진수정'에 만든다.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from api.images import ImageApi
from api.products import ProductApi
from services import detail_page
from services.automation import runner
from services.automation.config import AutomationSettings, load_settings
from services.automation.input_sheet import read_items
from services.automation.pipeline import PreparedItem, build_context, item_image_options, prepare
from services.images import MAX_PRODUCT_IMAGES, SOURCE_EXTENSIONS
from services.images.processing import load_image, process_product, save_jpeg, to_square
from storage.db import app_data_dir

log = logging.getLogger(__name__)


class StoreEditError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 안내 문구."""


# --- 공통 ---
def seller_code(raw: dict) -> str:
    info = ((raw.get("originProduct") or {}).get("detailAttribute") or {}).get("sellerCodeInfo") or {}
    return (info.get("sellerManagementCode") or "").strip()


def find_code(db, origin_product_no: int, raw: dict) -> str:
    """판매자 상품코드(등록 때 엑셀 상품코드를 넣음), 없으면 자동화 기록에서 찾는다."""
    return seller_code(raw) or db.automation_code_for(origin_product_no) or ""


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_dir() -> Path:
    path = app_data_dir() / "상품수정_백업"
    path.mkdir(parents=True, exist_ok=True)
    return path


def put_origin(api: ProductApi, origin_product_no: int, body: dict) -> None:
    """원상품 전체를 다시 보낸다. 품절(OUTOFSTOCK)은 수정 요청에 보낼 수 없어 판매중으로 보낸다
    (재고가 0이면 네이버가 다시 품절로 둔다 — product_manager.build_update_body와 같은 규칙)."""
    op = body["originProduct"]
    if op.get("statusType") == "OUTOFSTOCK":
        op["statusType"] = "SALE"
    api.update_origin_product(origin_product_no, body)


# --- 상세페이지 템플릿 바꾸기 ---
def available_templates(settings: AutomationSettings) -> list[str]:
    """내장 템플릿 + 작업 엑셀 폴더의 '상세템플릿' 안 템플릿 (기본이 맨 앞)."""
    names = set()
    for folder in (detail_page.builtin_template_dir(), detail_page.user_template_dir(settings)):
        if Path(folder).is_dir():
            names |= {p.stem for p in Path(folder).glob("*.html")}
    rest = sorted(n for n in names if n != detail_page.DEFAULT_TEMPLATE)
    return ([detail_page.DEFAULT_TEMPLATE] if detail_page.DEFAULT_TEMPLATE in names else []) + rest


@dataclass
class DetailSource:
    """상세페이지를 다시 만드는 데 필요한 것 — 작업 엑셀 행 + 사진 폴더 + 스토어의 현재 상품명·가격."""
    code: str
    prepared: PreparedItem
    settings: AutomationSettings
    templates: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def current_template(self) -> str:
        return self.prepared.detail_template or detail_page.DEFAULT_TEMPLATE


def load_detail_source(db, origin_product_no: int, raw: dict) -> DetailSource:
    code = find_code(db, origin_product_no, raw)
    if not code:
        raise StoreEditError("판매자 상품코드가 없는 상품입니다.\n"
                             "상세페이지 다시 만들기는 자동화(작업 엑셀)로 등록한 상품만 할 수 있습니다.")
    settings = load_settings(db)
    try:
        items = read_items(settings.work_excel_path())
    except FileNotFoundError as e:
        raise StoreEditError(str(e)) from e
    item = next((i for i in items if i.code == code), None)
    if item is None:
        raise StoreEditError(f"작업 엑셀에 상품코드 {code} 행이 없습니다.\n({settings.work_excel_path()})\n"
                             "상세페이지는 작업 엑셀의 설명·실측·소재와 사진 폴더로 만듭니다.")
    warnings = []
    processed = process_product(settings.image_root_path(), code, item_image_options(settings, item),
                                remover=runner._bg_remover)   # 이미 보정한 사진은 건너뜀
    warnings += processed.errors
    p = prepare(item, build_context(db, items), allow_registered=True)
    if p.draft is None:
        raise StoreEditError("상세페이지를 만들 수 없습니다:\n" + "\n".join(f"• {e}" for e in p.errors))
    op = raw.get("originProduct") or {}
    # 스토어에서 고친 상품명·판매가가 상세페이지에 들어가도록 스토어 값을 쓴다
    p.draft.name = op.get("name") or p.draft.name
    p.draft.sale_price = int(op.get("salePrice") or p.draft.sale_price)
    warnings += [w for w in p.warnings if "설명(최종)" in w or "공지" in w]
    return DetailSource(code, p, settings, available_templates(settings), warnings)


def _check_template(source: DetailSource, template: str) -> None:
    if not detail_page.template_exists(source.settings, template):
        raise StoreEditError(f"상세 템플릿 '{template}'이(가) 없습니다.")
    images = source.prepared.images
    if template == detail_page.IMAGE_ONLY_TEMPLATE and not (images and images.detail):
        raise StoreEditError(f"'{template}' 템플릿은 상세 이미지가 필요합니다. "
                             f"파일명에 detail이 들어간 사진을 '{images.folder if images else '사진 폴더'}'에 넣으세요.")


def render_detail(source: DetailSource, template: str, image_src) -> str:
    _check_template(source, template)
    p = source.prepared
    settings = source.settings
    context = detail_page.build_context(p.draft, p.item, p.images, p.measurements, image_src,
                                        detail_page.notice_image_path(settings), detail_page.notice_text(settings))
    return detail_page.render(settings, template, context)


def preview_detail(source: DetailSource, template: str) -> Path:
    """PC의 사진을 직접 가리키는 미리보기 HTML 파일."""
    html = render_detail(source, template, detail_page.local_src)
    return detail_page.write_preview(f"{source.code}_{template}", html, source.prepared.draft.name)


def apply_detail(services, origin_product_no: int, source: DetailSource, template: str) -> dict:
    """사진을 올리고 고른 템플릿으로 상세페이지를 만들어 스토어 상품의 상세설명만 바꾼다. 새로 조회한 원상품을 돌려준다."""
    _check_template(source, template)
    api = ProductApi(services.client())
    raw = api.get_origin_product(origin_product_no)
    old = (raw.get("originProduct") or {}).get("detailContent") or ""
    backup = backup_dir() / f"{source.code}_상세페이지_{_stamp()}.html"
    backup.write_text(old, encoding="utf-8")

    notice = detail_page.notice_image_path(source.settings)
    to_upload = [str(x) for x in detail_page.page_images(source.prepared.images, notice)]
    uploaded = dict(zip(to_upload, ImageApi(services.client()).upload(to_upload))) if to_upload else {}
    html = render_detail(source, template, lambda path: uploaded[str(path)])

    body = copy.deepcopy(raw)
    body["originProduct"]["detailContent"] = html
    put_origin(api, origin_product_no, body)
    detail_page.save_registered(source.code, html, source.prepared.draft.name)
    log.info("상세페이지 변경: originProductNo=%s code=%s template=%s (이전 내용 백업 %s)",
             origin_product_no, source.code, template, backup.name)
    return api.get_origin_product(origin_product_no)


# --- 사진 바꾸기 ---
@dataclass
class PhotoEntry:
    url: str = ""     # 스토어에 이미 올라간 사진
    path: str = ""    # PC에서 새로 고른 사진 (적용할 때 보정·업로드)

    @property
    def is_new(self) -> bool:
        return bool(self.path)


def current_photos(raw: dict) -> list[PhotoEntry]:
    images = (raw.get("originProduct") or {}).get("images") or {}
    urls = [(images.get("representativeImage") or {}).get("url", "")]
    urls += [(x or {}).get("url", "") for x in images.get("optionalImages") or []]
    return [PhotoEntry(url=u) for u in urls if u]


def validate_photos(entries: list[PhotoEntry]) -> list[str]:
    errors = []
    if not entries:
        errors.append("대표 사진이 1장 있어야 합니다.")
    if len(entries) > MAX_PRODUCT_IMAGES:
        errors.append(f"사진은 대표 1장 + 추가 {MAX_PRODUCT_IMAGES - 1}장까지입니다. (지금 {len(entries)}장)")
    for e in entries:
        if e.is_new:
            path = Path(e.path)
            if not path.is_file():
                errors.append(f"사진 파일이 없습니다: {path}")
            elif path.suffix.lower() not in SOURCE_EXTENSIONS:
                errors.append(f"쓸 수 없는 사진 형식입니다: {path.name}")
    return errors


def photos_changed(raw: dict, entries: list[PhotoEntry]) -> bool:
    return any(e.is_new for e in entries) or [e.url for e in entries] != [e.url for e in current_photos(raw)]


def prepare_photo(src: Path, out_dir: Path, index: int, settings: AutomationSettings) -> Path:
    """새 사진을 정사각형(기본 1000×1000) JPEG로 맞춘다. 원본 파일은 읽기만 한다."""
    options = settings.image_options()
    img = to_square(load_image(src), options.size, options.square_mode)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{index:02d}_{src.stem}.jpg"
    save_jpeg(img, target, options.max_bytes)
    return target


def apply_photos(services, origin_product_no: int, entries: list[PhotoEntry]) -> tuple[dict, str]:
    """사진 순서·구성을 스토어에 반영한다. 첫 장이 대표 사진. (새로 조회한 원상품, 결과 문구)를 돌려준다."""
    errors = validate_photos(entries)
    if errors:
        raise StoreEditError("\n".join(errors))
    api = ProductApi(services.client())
    raw = api.get_origin_product(origin_product_no)
    if not photos_changed(raw, entries):
        return raw, "사진이 바뀌지 않았습니다."
    old = (raw.get("originProduct") or {}).get("images") or {}
    backup = backup_dir() / f"{origin_product_no}_사진_{_stamp()}.json"
    backup.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")

    settings = load_settings(services.db)
    work = app_data_dir() / "사진수정" / f"{origin_product_no}_{_stamp()}"
    new = [(i, prepare_photo(Path(e.path), work, i + 1, settings)) for i, e in enumerate(entries) if e.is_new]
    urls = [e.url for e in entries]
    if new:
        for (i, _), url in zip(new, ImageApi(services.client()).upload([p for _, p in new])):
            urls[i] = url

    body = copy.deepcopy(raw)
    images = body["originProduct"].setdefault("images", {})
    images["representativeImage"] = {"url": urls[0]}
    images["optionalImages"] = [{"url": u} for u in urls[1:]]
    put_origin(api, origin_product_no, body)
    log.info("사진 변경: originProductNo=%s %d장 (새 사진 %d장, 이전 목록 백업 %s)",
             origin_product_no, len(urls), len(new), backup.name)
    message = f"사진 {len(urls)}장 저장 (대표 1 + 추가 {len(urls) - 1})" + (f", 새 사진 {len(new)}장" if new else "")
    return api.get_origin_product(origin_product_no), message
