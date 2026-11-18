"""4단계: 상세페이지 — 템플릿 렌더링, 사이즈표·배송 안내, 사용자 템플릿, 미리보기, 등록 시 네이버 이미지 주소."""
import re
from pathlib import Path

import pytest
from urllib.parse import unquote, urlparse

from PIL import Image

from services import detail_page
from services.automation import config, runner
from services.automation.config import AutomationSettings
from services.automation.input_sheet import InputItem
from services.automation.parsing import parse_measurements
from services.images import product_images
from services.options import OptionGroup, generate_rows
from services.product_builder import ProductDraft
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, row, sheet  # noqa: F401


def draft(**kw):
    groups = [OptionGroup("색상", ["블랙", "화이트"]), OptionGroup("사이즈", ["S", "M"])]
    d = ProductDraft(name="코튼 티셔츠", category_name="패션의류>여성의류>티셔츠", use_options=True,
                     option_groups=groups, option_rows=generate_rows(groups), delivery_fee_type="CONDITIONAL_FREE",
                     base_fee=3000, free_condition_amount=50000, return_fee=3500, exchange_fee=7000,
                     as_phone="02-123-4567")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def item(**values):
    return InputItem(2, {"설명(최종)": "부드러운 면 소재\n<script>alert(1)</script>", "소재": "면 100%",
                         "세탁": "손세탁"} | values)


MARKERS = {   # 템플릿별 사이즈 제목, 구성 순서
    "기본": ("SIZE GUIDE", ["https://img/공지.png", "티셔츠", "코튼", "부드러운 면 소재", "https://img/01.jpg",
                           "블랙", "https://img/02.jpg", "SIZE GUIDE", "면 100%", "배송 · 교환 안내"]),
    "심플": ("SIZE (cm)", ["https://img/공지.png", "티셔츠", "코튼 티셔츠", "부드러운 면 소재", "블랙 / 화이트",
                          "https://img/01.jpg", "SIZE (cm)", "면 100%", "배송 · 교환 안내"]),
}


def render(tmp_path, d=None, it=None, measurements="S:어깨40,가슴50 / M:어깨42,총장70", notice=None,
           template="기본", photos=("https://img/01.jpg", "https://img/02.jpg")):
    settings = AutomationSettings(work_excel=str(tmp_path / "상품입력.xlsx"))
    ctx = detail_page.build_context(d or draft(), it or item(), None, parse_measurements(measurements),
                                    lambda p: f"https://img/{Path(p).name}", notice)
    ctx["photos"] = list(photos)
    return detail_page.render(settings, template, ctx)


@pytest.mark.parametrize("template", MARKERS)
def test_render_all_sections_in_order(tmp_path, template):
    html = render(tmp_path, notice=Path("공지.png"), template=template)
    positions = [html.index(x) for x in MARKERS[template][1]]
    assert positions == sorted(positions)
    assert "<script>" not in html and "&lt;script&gt;" in html          # 설명은 이스케이프
    assert "<link" not in html and "<style" not in html                 # 인라인 스타일만
    assert "&#39;" not in html.split(">")[0]                            # 글꼴 따옴표가 깨지지 않음


@pytest.mark.parametrize("template", MARKERS)
def test_size_table_fills_missing_values(tmp_path, template):
    html = render(tmp_path, template=template)
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    cells = [[re.sub(r"\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)] for r in rows]
    assert ["S", "40", "50", "-"] in cells and ["M", "42", "-", "70"] in cells
    header = re.findall(r"<th[^>]*>(.*?)</th>", html.split(MARKERS[template][0])[1].split("</tr>")[0])
    assert header == ["사이즈", "어깨", "가슴", "총장"]


@pytest.mark.parametrize("template", MARKERS)
def test_optional_sections_omitted(tmp_path, template):
    html = render(tmp_path, d=draft(use_options=False), it=InputItem(2, {}), measurements="", template=template)
    assert MARKERS[template][0] not in html and "소재 · 세탁" not in html and "공지" not in html
    assert "배송 · 교환 안내" in html                                    # 배송 안내는 항상


def test_split_headline_and_view_captions(tmp_path):
    assert detail_page.split_headline("플라워 쉬폰 롱 원피스") == ("플라워 쉬폰", "롱 원피스")
    assert detail_page.split_headline("꽈배기 케이블 니트") == ("꽈배기 케이블", "니트")
    assert detail_page.split_headline("니트") == ("니트", "")
    html = render(tmp_path, photos=[f"https://img/{i}.jpg" for i in range(5)])   # 대표 1 + 추가 4
    assert re.findall(r"VIEW (\d\d)", html) == ["01", "02", "03", "04"]
    odd = render(tmp_path, photos=[f"https://img/{i}.jpg" for i in range(4)])    # 추가 3장 → 마지막 줄 한 장
    assert re.findall(r"VIEW (\d\d)", odd) == ["01", "02", "03"]
    single = render(tmp_path, photos=["https://img/0.jpg"])
    assert "DETAIL VIEW" not in single


def test_delivery_rows():
    assert detail_page.delivery_rows(ProductDraft(delivery_fee_type="FREE"))[1] == ("배송비", "무료배송")
    assert detail_page.delivery_rows(ProductDraft(delivery_fee_type="PAID", base_fee=2500))[1][1] == "2,500원"
    assert detail_page.delivery_rows(draft())[1][1] == "3,000원 (50,000원 이상 구매 시 무료)"
    collect = ProductDraft(delivery_fee_type="PAID", base_fee=3000, delivery_fee_pay_type="COLLECT")
    assert detail_page.delivery_rows(collect)[1][1].endswith("착불")
    assert detail_page.delivery_rows(ProductDraft(delivery_company="HANJIN"))[0] == ("택배사", "한진택배")
    assert ("A/S 문의", "02-123-4567") in detail_page.exchange_rows(draft())


def test_user_template_overrides_builtin(tmp_path):
    settings = AutomationSettings(work_excel=str(tmp_path / "상품입력.xlsx"))
    assert detail_page.template_exists(settings, "기본") and not detail_page.template_exists(settings, "여름")
    folder = detail_page.user_template_dir(settings)
    folder.mkdir()
    (folder / "여름.html").write_text("<div>여름 {{ p.name }}</div>", encoding="utf-8")
    (folder / "기본.html").write_text("<div>내 기본 {{ p.name }}</div>", encoding="utf-8")
    assert detail_page.template_exists(settings, "여름")
    ctx = detail_page.build_context(draft(), item(), None, None, str)
    assert detail_page.render(settings, "여름", ctx) == "<div>여름 코튼 티셔츠</div>"
    assert detail_page.render(settings, "기본", ctx) == "<div>내 기본 코튼 티셔츠</div>"


# --- 자동화 흐름 연동 ---
def photos(root, code, names=("01.png", "02.png", "03_detail.png")):
    folder = root / "img" / code / "원본"
    folder.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        Image.new("RGB", (600, 600), (60 * i, 120, 200)).save(folder / n)


def test_missing_template_fails_validation(env):
    db, root, excel = env
    mappings = config.load_mappings(db)
    mappings["티셔츠"].detail_template = "없는템플릿"
    config.save_mappings(db, list(mappings.values()))
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    assert any("상세 템플릿 '없는템플릿'" in e for e in report.prepared[0].errors)


def test_dry_run_writes_local_preview(env):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001", 실측="S:어깨40", 소재="면")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    p = report.prepared[0]
    assert p.preview and p.preview.exists() and "상세페이지 미리보기: " in runner.format_report(report)
    html = p.preview.read_text(encoding="utf-8")
    srcs = re.findall(r'<img src="([^"]+)"', html)
    assert len(srcs) == 3 and all(s.startswith("file:///") for s in srcs)
    assert all(Path(unquote(urlparse(s).path).lstrip("/")).exists() for s in srcs)  # 실제 파일을 가리킴
    assert "SIZE GUIDE" in html and not (root / "img" / "A001" / "보정").exists()   # dry-run은 보정 안 함


def test_register_uses_uploaded_urls_in_detail(env):
    db, root, excel = env
    notice = root / "notice.png"
    Image.new("RGB", (860, 200), (250, 220, 0)).save(notice)
    s = config.load_settings(db)
    s.notice_image = str(notice)
    config.save_settings(db, s)
    photos(root, "A001")
    add_rows(excel, [row("A001", 실측="S:어깨40", 소재="면 100%", **{"설명(최종)": "좋은 옷"})])
    client = FakeClient()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    detail = client.registered[0]["originProduct"]["detailContent"]
    srcs = re.findall(r'<img src="([^"]+)"', detail)
    assert srcs == [f"https://shop-phinf/{i}" for i in range(4)]       # 공지 → 대표 → 추가 → 상세용
    assert "file:" not in detail and "\\" not in detail and "좋은 옷" in detail and "SIZE GUIDE" in detail
    images = client.registered[0]["originProduct"]["images"]
    assert images["representativeImage"]["url"] == "https://shop-phinf/1"   # 같은 주소 재사용
    saved = detail_page.app_data_dir() / "상세페이지_등록본" / "A001.html"
    assert saved.exists() and "https://shop-phinf/0" in saved.read_text(encoding="utf-8")
    assert sheet(excel)["A001"][0] == "등록완료(판매중지)"


def test_preview_pages_processes_photos(env, capsys):
    import cli
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    add_rows(excel, [row("A001"), row("A002", 상태="판매중")])
    results = runner.preview_pages(db)
    assert [p.code for p in results] == ["A001"] and results[0].preview.exists()
    assert product_images(root / "img", "A001").edited                    # 사진 보정 후 미리보기
    assert "/%EB%B3%B4%EC%A0%95/" in results[0].preview.read_text(encoding="utf-8")  # '보정' 폴더 사진
    assert cli.main(["preview", "--code", "A002", "--no-process"]) == 0
    out = capsys.readouterr().out
    assert "A002" in out and "1/1개 미리보기 생성" in out


@pytest.mark.parametrize("template", MARKERS)
def test_notice_banner_text_and_image_priority(tmp_path, template):
    settings = AutomationSettings(work_excel=str(tmp_path / "상품입력.xlsx"))

    def page(notice=None, text=("", "")):
        ctx = detail_page.build_context(draft(), item(), None, None, lambda p: f"https://img/{Path(p).name}",
                                        notice, text)
        return detail_page.render(settings, template, ctx)

    banner = page(text=("오늘 주문 시 내일 출발", "5만원 이상 <무료배송>"))
    assert "오늘 주문 시 내일 출발" in banner and "5만원 이상 &lt;무료배송&gt;" in banner   # 문구 띠 (이스케이프)
    assert banner.index("오늘 주문 시 내일 출발") < banner.index("코튼")                 # 맨 위
    only_title = page(text=("오늘 출발", ""))
    assert "오늘 출발" in only_title
    image = page(notice=Path("공지.png"), text=("오늘 주문 시 내일 출발", ""))
    assert "https://img/공지.png" in image and "오늘 주문 시 내일 출발" not in image      # 이미지가 우선
    none = page()
    assert "NOTICE" not in none and "공지" not in none


def test_notice_text_reaches_registered_detail(env):
    db, root, excel = env
    s = config.load_settings(db)
    s.notice_title, s.notice_subtitle = "오늘 주문 시 내일 출발", "5만원 이상 무료배송"
    config.save_settings(db, s)
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    client = FakeClient()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    detail = client.registered[0]["originProduct"]["detailContent"]
    assert "오늘 주문 시 내일 출발" in detail and "5만원 이상 무료배송" in detail
    assert len(re.findall(r'<img src="https://shop-phinf/', detail)) == 3            # 공지는 이미지가 아니라 글자


def test_detail_photos_in_card_section(tmp_path):
    settings = AutomationSettings(work_excel=str(tmp_path / "상품입력.xlsx"))
    ctx = detail_page.build_context(draft(), item(), None, None, str)
    ctx["photos"], ctx["detail_photos"] = ["https://img/01.jpg"], ["https://img/d1.jpg", "https://img/d2.jpg"]
    html = detail_page.render(settings, "기본", ctx)
    assert html.index("DETAIL CUT") < html.index("https://img/d1.jpg") < html.index("https://img/d2.jpg")
    for src in ("d1", "d2"):                                   # 다른 사진과 같은 둥근 카드
        tag = re.search(rf'<img src="https://img/{src}.jpg"[^>]*>', html).group(0)
        assert "border-radius:18px" in tag and "border:1px solid" in tag
    ctx["detail_photos"] = []
    assert "DETAIL CUT" not in detail_page.render(settings, "기본", ctx)


def test_template_images_are_resolved_from_template_folder(tmp_path):
    """템플릿에 직접 넣은 사진(템플릿 파일 기준 상대 경로)은 미리보기에선 PC 주소, 등록 땐 올린 주소로 바꾼다."""
    settings = AutomationSettings(work_excel=str(tmp_path / "상품입력.xlsx"))
    folder = detail_page.user_template_dir(settings)
    (folder / "images").mkdir(parents=True)
    for name in ("모델 컷.jpg", "bg.png"):
        Image.new("RGB", (10, 10)).save(folder / "images" / name)
    (folder / "룩북.html").write_text(
        '<style>.hero{background:url("images/bg.png")}</style>'
        '<img src="images/%EB%AA%A8%EB%8D%B8%20%EC%BB%B7.jpg"><button data-img="images/모델 컷.jpg"></button>'
        '<img src="https://cdn.example.com/a.jpg"><img src="{{ p.photos[0] }}">', encoding="utf-8")
    ctx = detail_page.build_context(draft(), item(), None, None, str)
    ctx["photos"] = ["https://img/01.jpg"]

    preview = detail_page.render(settings, "룩북", ctx)
    assert 'src="file:///' in preview and 'url("file:///' in preview and '"images/' not in preview

    calls = []
    def upload(paths):
        calls.append([p.name for p in paths])
        return [f"https://shop-phinf/{i}" for i in range(len(paths))]
    html = detail_page.render(settings, "룩북", ctx, image_urls=upload)
    assert calls == [["모델 컷.jpg", "bg.png"]]                       # 같은 파일은 한 번만
    assert html.count("https://shop-phinf/0") == 2 and 'url("https://shop-phinf/1")' in html
    assert "https://cdn.example.com/a.jpg" in html and "https://img/01.jpg" in html   # 인터넷 주소는 그대로

    (folder / "없음.html").write_text('<img src="images/없는사진.jpg">', encoding="utf-8")
    with pytest.raises(detail_page.TemplateImageError, match="없는사진.jpg"):
        detail_page.render(settings, "없음", ctx)
    Image.new("RGB", (10, 10)).save(folder / "images" / "a.webp")
    (folder / "웹피.html").write_text('<img src="images/a.webp">', encoding="utf-8")
    with pytest.raises(detail_page.TemplateImageError, match="JPG"):
        detail_page.render(settings, "웹피", ctx)


def test_store_removes_style_and_script_so_preview_matches(tmp_path):
    """스토어는 <style>·<script>·<link>를 지운다 → 미리보기도 똑같이 빼고 경고, 등록·적용은 막는다."""
    page = ('<link rel="stylesheet" href="a.css"><style>.t{color:red}</style>'
            '<div class="t" style="color:#111">제목</div><script>alert(1)</script>')
    assert detail_page.store_removed_parts(page) == ["<style> (CSS 블록)", "<script> (스크립트)", "<link> (외부 CSS)"]
    assert detail_page.store_view(page) == '<div class="t" style="color:#111">제목</div>'
    with pytest.raises(detail_page.TemplateStyleError, match="인라인"):
        detail_page.check_for_store(page, "퀸")
    preview = detail_page.write_preview("X1", page, "제목").read_text(encoding="utf-8")
    assert "<style>.t" not in preview and "alert(1)" not in preview and "스토어에 적용할 수 없습니다" in preview

    inline = '<div style="color:#111">제목</div>'
    detail_page.check_for_store(inline)                      # 인라인 스타일만이면 통과
    assert "적용할 수 없습니다" not in detail_page.write_preview("X2", inline, "제목").read_text(encoding="utf-8")
    for name in ("기본", "심플", "이미지만"):                   # 내장 템플릿은 모두 스토어에 그대로 남는다
        source = (detail_page.builtin_template_dir() / f"{name}.html").read_text(encoding="utf-8")
        assert not detail_page.store_removed_parts(source), name
