"""상세페이지 조립 — 분류별 Jinja2 템플릿 + 상품 정보 → 상세설명 HTML.

템플릿 찾는 순서: {작업 엑셀 폴더}/상세템플릿/{이름}.html → 앱 내장 resources/templates/{이름}.html
구성: 상단 공지 이미지 → 설명 → 옵션 → 사진 → 사이즈표(실측) → 소재·세탁 → 배송·교환 안내

등록용 HTML은 네이버 이미지 업로드 API로 올린 주소를 쓰고, 미리보기 HTML은 PC의 사진 파일을 직접 가리킨다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from services.automation.config import AutomationSettings
from services.automation.input_sheet import InputItem
from services.automation.parsing import MeasurementsParsed
from services.images import ImageSet
from services.paths import resource_path
from services.product_builder import DELIVERY_COMPANIES, ProductDraft
from storage.db import app_data_dir

USER_TEMPLATE_DIR = "상세템플릿"
DEFAULT_TEMPLATE = "기본"
IMAGE_ONLY_TEMPLATE = "이미지만"   # 완성된 상세 이미지(파일명에 detail)만 차례로 넣는 내장 템플릿
_COMPANY_NAMES = {code: name for name, code in DELIVERY_COMPANIES}


def user_template_dir(settings: AutomationSettings) -> Path:
    return settings.work_excel_path().parent / USER_TEMPLATE_DIR


def builtin_template_dir() -> Path:
    return resource_path("templates")


def _environment(settings: AutomationSettings) -> Environment:
    loader = ChoiceLoader([FileSystemLoader(str(user_template_dir(settings)), encoding="utf-8"),
                           FileSystemLoader(str(builtin_template_dir()), encoding="utf-8")])
    return Environment(loader=loader, autoescape=True, undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True)


def template_exists(settings: AutomationSettings, name: str) -> bool:
    try:
        _environment(settings).get_template(f"{name or DEFAULT_TEMPLATE}.html")
        return True
    except TemplateNotFound:
        return False


def won(value: int) -> str:
    return f"{value:,}원"


def delivery_rows(d: ProductDraft) -> list[tuple[str, str]]:
    if d.delivery_fee_type == "FREE":
        fee = "무료배송"
    elif d.delivery_fee_type == "CONDITIONAL_FREE":
        fee = f"{won(d.base_fee)} ({won(d.free_condition_amount)} 이상 구매 시 무료)"
    else:
        fee = won(d.base_fee)
    if d.delivery_fee_type != "FREE" and d.delivery_fee_pay_type == "COLLECT":
        fee += " · 착불"
    return [("택배사", _COMPANY_NAMES.get(d.delivery_company, d.delivery_company)), ("배송비", fee)]


def exchange_rows(d: ProductDraft) -> list[tuple[str, str]]:
    rows = [("반품 배송비", f"편도 {won(d.return_fee)}"), ("교환 배송비", f"왕복 {won(d.exchange_fee)}")]
    if d.as_phone:
        rows.append(("A/S 문의", d.as_phone + (f" ({d.as_guide})" if d.as_guide else "")))
    return rows


def size_table(measurements: MeasurementsParsed | None) -> dict:
    if not measurements or not measurements.sizes:
        return {"columns": [], "rows": []}
    columns = measurements.columns()
    rows = []
    for size, items in measurements.sizes.items():
        values = dict(items)
        rows.append((size, [values.get(col, "-") for col in columns]))
    return {"columns": columns, "rows": rows}


def split_headline(name: str) -> tuple[str, str]:
    """상품명을 앞(강조색)·뒤 두 부분으로 나눈다. 단어 수 기준 앞쪽이 절반(홀수면 하나 더)."""
    words = name.split()
    if len(words) < 2:
        return name, ""
    cut = (len(words) + 1) // 2
    return " ".join(words[:cut]), " ".join(words[cut:])


def build_context(draft: ProductDraft, item: InputItem, images: ImageSet | None,
                  measurements: MeasurementsParsed | None, image_src: Callable[[Path], str],
                  notice_image: Path | None = None, notice_text: tuple[str, str] = ("", "")) -> dict:
    photos, detail_photos = [], []
    if images and images.found:
        photos = [image_src(p) for p in [images.representative, *images.optional]]
        detail_photos = [image_src(p) for p in images.detail]
    lines = [line.strip() for line in item.get("설명(최종)").splitlines() if line.strip()]
    head, tail = split_headline(draft.name)
    return {
        "name": draft.name,
        "name_head": head,
        "name_tail": tail,
        "description_lead": lines[0] if lines else "",
        "description_rest": lines[1:],
        "category": draft.category_name.split(">")[-1] if draft.category_name else "",
        "notice_image": image_src(notice_image) if notice_image else "",
        "notice_title": notice_text[0].strip(),
        "notice_subtitle": notice_text[1].strip(),
        "description_lines": lines,
        "options": [(g.name, g.values) for g in draft.option_groups] if draft.use_options else [],
        "photos": photos,
        "detail_photos": detail_photos,
        "size_table": size_table(measurements),
        "material": item.get("소재"),
        "washing": item.get("세탁"),
        "delivery": delivery_rows(draft),
        "exchange": exchange_rows(draft),
    }


class TemplateImageError(ValueError):
    """템플릿에 직접 넣은 사진을 쓸 수 없을 때 (파일 없음·형식)."""


# 템플릿에 직접 넣은 사진 주소: src="..." / data-img="..." 같은 속성, CSS url(...)
_ASSET_ATTR = re.compile(r"""(\b(?:src|data-src|data-img|poster|background)\s*=\s*(["']))([^"']*)\2""", re.IGNORECASE)
_ASSET_CSS = re.compile(r"""(url\(\s*(["']?))([^"')]*)\2\s*\)""", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
UPLOADABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}   # 네이버 이미지 업로드가 받는 형식


def _local_ref(ref: str, base_dir: Path) -> Path | None:
    """PC 파일을 가리키는 주소면 그 경로 (템플릿 파일 위치 기준 상대 경로 포함). 인터넷 주소 등은 None."""
    ref = ref.strip()
    if not ref or ref.startswith(("#", "//")):
        return None
    if _SCHEME.match(ref) and not _WINDOWS_PATH.match(ref):
        return None   # https:, data:, file:(미리보기에서 이미 PC 주소로 바꾼 것)
    ref = unquote(ref.split("#", 1)[0].split("?", 1)[0])
    return (base_dir / ref).resolve() if ref else None


def template_images(html: str, base_dir: Path) -> list[Path]:
    """HTML 안에서 PC의 사진 파일을 가리키는 주소들 (중복 없이 나온 순서대로). 파일이 없거나 못 쓰는 형식이면 오류."""
    found: dict[Path, str] = {}
    for pattern in (_ASSET_ATTR, _ASSET_CSS):
        for m in pattern.finditer(html):
            path = _local_ref(m[3], base_dir)
            if path is not None:
                found.setdefault(path, m[3].strip())
    missing = [ref for path, ref in found.items() if not path.is_file()]
    if missing:
        raise TemplateImageError(f"상세 템플릿에 넣은 사진 파일이 없습니다 ('{base_dir}' 기준): " + ", ".join(missing))
    bad = [ref for path, ref in found.items() if path.suffix.lower() not in UPLOADABLE_EXTENSIONS]
    if bad:
        raise TemplateImageError("상세 템플릿 사진은 JPG·PNG·GIF·BMP만 쓸 수 있습니다: " + ", ".join(bad))
    return list(found)


def _replace_images(html: str, base_dir: Path, urls: dict[Path, str]) -> str:
    def swap(m: re.Match) -> str:
        path = _local_ref(m[3], base_dir)
        if path is None or path not in urls:
            return m[0]
        return m[1] + urls[path] + m[0][len(m[1]) + len(m[3]):]
    return _ASSET_CSS.sub(swap, _ASSET_ATTR.sub(swap, html))


def local_urls(paths: list[Path]) -> list[str]:
    return [local_src(p) for p in paths]


def render(settings: AutomationSettings, template_name: str, context: dict,
           image_urls: Callable[[list[Path]], list[str]] = local_urls) -> str:
    """템플릿으로 상세페이지 HTML을 만든다.

    템플릿에 직접 넣은 사진(예: images/모델컷.jpg — 템플릿 파일 위치 기준)은 image_urls로 주소를 바꾼다.
    기본은 PC 파일 주소(미리보기용)이고, 스토어에 보낼 때는 네이버 이미지 업로드 함수를 넘긴다.
    """
    template = _environment(settings).get_template(f"{template_name or DEFAULT_TEMPLATE}.html")
    html = template.render(p=context).strip()
    base_dir = Path(template.filename).parent
    paths = template_images(html, base_dir)
    if not paths:
        return html
    return _replace_images(html, base_dir, dict(zip(paths, image_urls(paths))))


def notice_image_path(settings: AutomationSettings) -> Path | None:
    path = Path(settings.notice_image) if settings.notice_image else None
    return path if path and path.is_file() else None


def notice_text(settings: AutomationSettings) -> tuple[str, str]:
    return settings.notice_title, settings.notice_subtitle


def page_images(images: ImageSet | None, notice: Path | None) -> list[Path]:
    """상세페이지에 들어가는 사진 (업로드 대상)."""
    result = [notice] if notice else []
    if images and images.found:
        result += [images.representative, *images.optional, *images.detail]
    return result


def preview_dir() -> Path:
    path = app_data_dir() / "상세페이지_미리보기"
    path.mkdir(parents=True, exist_ok=True)
    return path


class TemplateStyleError(ValueError):
    """스토어 상세설명에서 지워지는 <style>·<script>·<link>를 쓰는 템플릿."""


# 네이버 상세설명은 <style>·<script>·<link>를 지우고 태그의 class만 남긴다 → 디자인이 빠진 글자만 보인다.
# 남는 꾸밈은 각 태그의 style="..."(인라인) 뿐이다. (2026-11-18 퀸_상세템플릿 적용 결과로 확인)
_STORE_REMOVES = re.compile(r"<(style|script)\b[^>]*>.*?</\1\s*>|<(?:style|script)\b[^>]*/?>|<link\b[^>]*>",
                            re.IGNORECASE | re.DOTALL)
_STORE_REMOVED_TAGS = (("style", "<style> (CSS 블록)"), ("script", "<script> (스크립트)"), ("link", "<link> (외부 CSS)"))


def store_removed_parts(html: str) -> list[str]:
    """스토어에서 지워져 미리보기와 다르게 보이게 만드는 부분."""
    return [label for tag, label in _STORE_REMOVED_TAGS if re.search(rf"<{tag}\b", html, re.IGNORECASE)]


def store_view(html: str) -> str:
    """스토어에 실제로 남는 모양 (지워지는 부분을 뺀 HTML)."""
    return _STORE_REMOVES.sub("", html)


def check_for_store(html: str, template_name: str = "") -> None:
    parts = store_removed_parts(html)
    if parts:
        name = f"'{template_name}' 템플릿" if template_name else "이 템플릿"
        raise TemplateStyleError(
            f"{name}은(는) 스마트스토어가 지우는 {', '.join(parts)}로 꾸며져 있어 스토어에서는 디자인이 빠진 글자만 보입니다.\n"
            "꾸밈을 모두 각 태그의 style=\"...\"(인라인 스타일)로 넣은 템플릿을 쓰세요. (예: 기본, 심플)")


def write_preview(code: str, html: str, title: str) -> Path:
    """브라우저로 열어 볼 수 있는 완전한 HTML 파일로 저장한다.

    스토어가 지우는 <style>·<script>·<link>는 미리보기에서도 빼서 스토어와 같은 모양으로 보여 주고, 맨 위에 알린다.
    """
    removed = store_removed_parts(html)
    banner = ("<div style='background:#B3261E;color:#fff;font-weight:700;font-size:15px;line-height:1.6;"
              "padding:14px 20px;margin:0 0 20px;'>⚠ 이 템플릿은 스마트스토어가 지우는 "
              f"{', '.join(removed)}를 써서 스토어에서는 아래처럼 디자인이 빠져 보입니다. "
              "스토어에 적용할 수 없습니다.</div>") if removed else ""
    path = preview_dir() / f"{code}.html"
    path.write_text("<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>{title} - 상세페이지 미리보기</title></head>"
                    "<body style='margin:0;background:#eee;'><div style='background:#fff;max-width:900px;"
                    f"margin:0 auto;padding:20px 0;'>{banner}{store_view(html)}</div></body></html>", encoding="utf-8")
    return path


def local_src(path: Path) -> str:
    return path.resolve().as_uri()


def save_registered(code: str, html: str, title: str) -> Path:
    """실제 등록에 쓴 상세페이지(네이버 이미지 주소)를 기록용으로 저장한다."""
    folder = app_data_dir() / "상세페이지_등록본"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{code}.html"
    path.write_text(f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>{title}</title></head>"
                    f"<body>{html}</body></html>", encoding="utf-8")
    return path
