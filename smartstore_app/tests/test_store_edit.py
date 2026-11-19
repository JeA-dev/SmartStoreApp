"""스토어 상품의 상세페이지 템플릿 바꾸기 · 사진 바꾸기 (services/store_edit.py, 수정 창 탭)."""
import copy
import json
import os
import re
from pathlib import Path

import pytest
from PIL import Image

from services import detail_page, store_edit
from services.automation import pipeline
from services.automation.config import load_settings
from services.automation.input_sheet import read_items
from services.store_edit import PhotoEntry, StoreEditError
from tests.test_automation_register import add_rows, env, row  # noqa: F401

NO = 777


def photos(root, code, names=("01.png", "02.png", "03_detail.png")):
    folder = root / "img" / code / "원본"
    folder.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        Image.new("RGB", (600, 400), (60 * i, 120, 200)).save(folder / n)
    return folder


def store_raw(code="A001", status="OUTOFSTOCK"):
    """스토어 원상품 조회 응답(줄인 형태). 사람이 스토어에서 상품명·가격을 고쳐 둔 상태."""
    return {
        "originProduct": {
            "statusType": status, "name": "스토어에서 고친 이름", "salePrice": 25000, "stockQuantity": 0,
            "detailContent": "<p>예전 상세페이지</p>",
            "images": {"representativeImage": {"url": "https://shop-phinf/old0"},
                       "optionalImages": [{"url": "https://shop-phinf/old1"}, {"url": "https://shop-phinf/old2"}]},
            "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": code} if code else {},
                                "afterServiceInfo": {"afterServiceTelephoneNumber": "02-123-4567"}},
        },
        "smartstoreChannelProduct": {"channelProductDisplayStatusType": "ON"},
    }


class StoreClient:
    """원상품 조회·수정, 이미지 업로드 대역."""

    def __init__(self, raw):
        self.raw = raw
        self.puts, self.uploads = [], []

    def get(self, path, params=None):
        assert path.endswith(f"/origin-products/{NO}")
        return copy.deepcopy(self.raw)

    def post(self, path, json=None, files=None):
        assert path.endswith("/product-images/upload")
        start = len(self.uploads)
        self.uploads += [f[1][0] for f in files]
        return {"images": [{"url": f"https://shop-phinf/new{start + i}"} for i in range(len(files))]}

    def put(self, path, json=None):
        assert path.endswith(f"/origin-products/{NO}")
        self.puts.append(copy.deepcopy(json))
        self.raw = copy.deepcopy(json)
        self.raw["originProduct"]["statusType"] = "OUTOFSTOCK"   # 재고 0이면 네이버가 다시 품절로 둔다


class Services:
    def __init__(self, db, client):
        self.db, self._client = db, client

    def client(self):
        return self._client


def setup_item(env, code="A001", **kw):
    db, root, excel = env
    photos(root, code)
    add_rows(excel, [row(code, 상태="판매중", 실측="S:어깨40", 소재="면 100%", **{"설명(최종)": "좋은 옷"}) | kw])
    db.upsert_automation_item(code, "판매중", origin_product_no=NO)


# --- 공통 ---
def test_find_code_from_seller_code_then_records(env):
    db, _, _ = env
    assert store_edit.find_code(db, NO, store_raw("L001")) == "L001"
    assert store_edit.find_code(db, NO, store_raw(None)) == ""
    db.upsert_automation_item("A009", "판매중", origin_product_no=NO)
    assert store_edit.find_code(db, NO, store_raw(None)) == "A009"
    db.upsert_automation_item("A009", "삭제됨")
    assert store_edit.find_code(db, NO, store_raw(None)) == ""        # 지운 상품 기록은 쓰지 않음


def test_prepare_allows_registered_code_only_when_asked(env):
    db, root, excel = env
    setup_item(env)
    items = read_items(excel)
    assert any("이미 등록된" in e for e in pipeline.prepare(items[0], pipeline.build_context(db, items)).errors)
    p = pipeline.prepare(items[0], pipeline.build_context(db, items), allow_registered=True)
    assert p.ok and p.draft is not None


def test_available_templates_builtin_and_user(env):
    db, _, _ = env
    settings = load_settings(db)
    folder = detail_page.user_template_dir(settings)
    folder.mkdir()
    (folder / "여름.html").write_text("<div>{{ p.name }}</div>", encoding="utf-8")
    names = store_edit.available_templates(settings)
    assert names[0] == "기본" and {"심플", "이미지만", "여름"} <= set(names)


# --- 상세페이지 ---
def test_detail_source_errors(env):
    db, _, _ = env
    with pytest.raises(StoreEditError, match="판매자 상품코드가 없는"):
        store_edit.load_detail_source(db, NO, store_raw(None))
    with pytest.raises(StoreEditError, match="작업 엑셀에 상품코드 Z999"):
        store_edit.load_detail_source(db, NO, store_raw("Z999"))


def test_image_only_template_needs_detail_photos(env):
    db, root, excel = env
    photos(root, "A002", names=("01.png", "02.png"))
    add_rows(excel, [row("A002", 상태="판매중")])
    source = store_edit.load_detail_source(db, NO, store_raw("A002"))
    with pytest.raises(StoreEditError, match="상세 사진이 필요"):
        store_edit.preview_detail(source, "이미지만")
    assert store_edit.preview_detail(source, "심플").exists()


def test_preview_uses_store_name_and_local_photos(env):
    db, _, _ = env
    setup_item(env)
    source = store_edit.load_detail_source(db, NO, store_raw())
    assert source.code == "A001" and source.current_template == "기본"
    html = store_edit.preview_detail(source, "심플").read_text(encoding="utf-8")
    assert "스토어에서 고친 이름" in html and "좋은 옷" in html
    assert all(s.startswith("file:///") for s in re.findall(r'<img src="([^"]+)"', html))


def test_apply_detail_changes_only_detail_content(env):
    db, _, _ = env
    setup_item(env)
    client = StoreClient(store_raw())
    services = Services(db, client)
    source = store_edit.load_detail_source(db, NO, store_raw())
    fresh = store_edit.apply_detail(services, NO, source, "심플")

    assert len(client.puts) == 1
    body = client.puts[0]["originProduct"]
    srcs = re.findall(r'<img src="([^"]+)"', body["detailContent"])
    assert srcs and all(s.startswith("https://shop-phinf/new") for s in srcs)
    assert "file:" not in body["detailContent"] and "스토어에서 고친 이름" in body["detailContent"]
    assert body["statusType"] == "SALE"                    # 품절은 수정 요청에 보낼 수 없음 → 판매중으로
    before = store_raw()["originProduct"]
    for key in ("name", "salePrice", "images", "detailAttribute"):   # 다른 정보는 그대로
        assert body[key] == before[key]
    assert fresh["originProduct"]["detailContent"] == body["detailContent"]
    backups = list(store_edit.backup_dir().glob("A001_상세페이지_*.html"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "<p>예전 상세페이지</p>"


# --- 사진 ---
def test_current_and_validate_photos(tmp_path):
    entries = store_edit.current_photos(store_raw())
    assert [e.url for e in entries] == ["https://shop-phinf/old0", "https://shop-phinf/old1", "https://shop-phinf/old2"]
    assert store_edit.validate_photos([]) == ["대표 사진이 1장 있어야 합니다."]
    assert "10장" not in " ".join(store_edit.validate_photos(entries))
    too_many = [PhotoEntry(url=f"u{i}") for i in range(11)]
    assert any("추가 9장까지" in e for e in store_edit.validate_photos(too_many))
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    assert any("형식" in e for e in store_edit.validate_photos([PhotoEntry(path=str(txt))]))
    assert any("없습니다" in e for e in store_edit.validate_photos([PhotoEntry(path=str(tmp_path / "no.jpg"))]))


def test_apply_photos_reorders_and_uploads_only_new(env, tmp_path):
    db, _, _ = env
    client = StoreClient(store_raw(status="SALE"))
    new_src = tmp_path / "새사진.png"
    Image.new("RGB", (1600, 900), (200, 30, 30)).save(new_src)
    before = new_src.read_bytes()
    entries = [PhotoEntry(url="https://shop-phinf/old2"), PhotoEntry(path=str(new_src)),
               PhotoEntry(url="https://shop-phinf/old0")]          # old1은 뺌
    fresh, message = store_edit.apply_photos(Services(db, client), NO, entries)

    images = client.puts[0]["originProduct"]["images"]
    assert images["representativeImage"] == {"url": "https://shop-phinf/old2"}
    assert images["optionalImages"] == [{"url": "https://shop-phinf/new0"}, {"url": "https://shop-phinf/old0"}]
    assert client.puts[0]["originProduct"]["detailContent"] == "<p>예전 상세페이지</p>"   # 상세페이지는 그대로
    assert client.uploads == ["02_새사진.jpg"] and "새 사진 1장" in message
    assert new_src.read_bytes() == before                                    # 원본은 그대로
    made = list((store_edit.app_data_dir() / "사진수정").rglob("02_새사진.jpg"))
    assert len(made) == 1 and Image.open(made[0]).size == (1000, 1000)
    backup = list(store_edit.backup_dir().glob(f"{NO}_사진_*.json"))
    assert len(backup) == 1 and json.loads(backup[0].read_text(encoding="utf-8")) == store_raw()["originProduct"]["images"]
    assert fresh["originProduct"]["images"] == images


def test_apply_photos_without_change_sends_nothing(env):
    db, _, _ = env
    client = StoreClient(store_raw())
    raw, message = store_edit.apply_photos(Services(db, client), NO, store_edit.current_photos(store_raw()))
    assert client.puts == [] and client.uploads == [] and "바뀌지 않았습니다" in message
    with pytest.raises(StoreEditError):
        store_edit.apply_photos(Services(db, client), NO, [])


# --- 화면 ---
def test_edit_dialog_tabs(env, monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    import ui.store_edit_tabs as tabs
    from services.product_manager import ProductEdit
    from ui.product_edit_dialog import ProductEditDialog
    app = QApplication.instance() or QApplication([])  # noqa: F841
    db, _, _ = env
    setup_item(env)
    client = StoreClient(store_raw(status="SALE"))
    monkeypatch.setattr(tabs, "run_async", lambda fn, done, fail: done(fn()))      # 스레드 없이
    monkeypatch.setattr(tabs, "fetch_images", lambda urls: {})                    # 네트워크 없이
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    raw = client.get(f"/x/origin-products/{NO}")
    edit = ProductEdit(NO, raw["originProduct"]["name"], 25000, 0, "SALE", "ON", raw=raw)
    dialog = ProductEditDialog(Services(db, client), edit)
    assert [dialog.tabs.tabText(i) for i in range(3)] == ["기본 정보", "상세페이지", "사진"]

    dialog.tabs.setCurrentIndex(1)                                   # 탭을 열면 불러옴
    assert not dialog.save_button.isEnabled()
    detail = dialog.detail_tab
    assert detail.source and detail.current_template() == "기본" and "A001" in detail.info.text()
    assert detail.template_combo.currentText() == "기본  (앱 기본)" and not detail.delete_btn.isEnabled()

    folder = load_settings(db).work_excel_path().parent / detail_page.USER_TEMPLATE_DIR   # 직접 만든 템플릿 삭제
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "지울것.html").write_text("<div>{{ p.name }}</div>", encoding="utf-8")
    detail._fill_templates("지울것")
    assert detail.template_combo.currentText() == "지울것" and detail.delete_btn.isEnabled()
    detail.on_delete_template()
    assert detail.template_combo.findData("지울것") < 0 and (folder / "삭제한_템플릿").is_dir()
    assert detail.current_template() == "기본"

    detail.template_combo.setCurrentIndex(detail.template_combo.findData("심플"))
    detail.on_apply()
    assert len(client.puts) == 1 and dialog.saved_actions == ["상세페이지 → 심플"]
    assert dialog.edit.raw["originProduct"]["detailContent"] == client.puts[0]["originProduct"]["detailContent"]

    dialog.tabs.setCurrentIndex(2)
    photo = dialog.photo_tab
    assert photo.list.count() == 3 and photo.list.item(0).text() == "대표"
    photo.list.setCurrentRow(2)
    photo.list.move_selected(target=0)                                # 대표 사진으로
    assert photo.list.item(0).text() == "대표" and photo.list.entries()[0].url == "https://shop-phinf/old2"
    new_src = tmp_path / "추가.png"
    Image.new("RGB", (500, 500), (0, 0, 0)).save(new_src)
    assert photo.list.add_files([new_src, tmp_path / "x.txt"]) == [tmp_path / "x.txt"]
    photo.list.relabel()
    assert photo.list.item(3).text() == "추가 3 · 새 사진"
    photo.on_apply()
    images = client.puts[1]["originProduct"]["images"]
    assert images["representativeImage"]["url"] == "https://shop-phinf/old2" and len(images["optionalImages"]) == 3
    assert "심플" not in images and client.puts[1]["originProduct"]["detailContent"] == \
        client.puts[0]["originProduct"]["detailContent"]              # 사진 바꿔도 앞서 바꾼 상세페이지 유지
    dialog.tabs.setCurrentIndex(0)
    assert dialog.save_button.isEnabled()
    dialog._close()
    assert dialog.result() == 1 and len(dialog.saved_actions) == 2   # 반영한 게 있으면 목록 새로 고침


def test_apply_detail_uploads_images_written_in_user_template(env):
    """사용자 템플릿에 넣은 images/... 사진도 올려서 네이버 주소로 보낸다 (그대로 보내면 InvalidImageUrl)."""
    db, _, excel = env
    setup_item(env)
    folder = excel.parent / detail_page.USER_TEMPLATE_DIR
    (folder / "images").mkdir(parents=True)
    Image.new("RGB", (20, 20)).save(folder / "images" / "model.jpg")
    (folder / "룩북.html").write_text('<div>{{ p.name }}<img src="images/model.jpg"></div>', encoding="utf-8")
    (folder / "깨짐.html").write_text('<img src="images/none.jpg">', encoding="utf-8")
    client = StoreClient(store_raw())
    source = store_edit.load_detail_source(db, NO, store_raw())
    assert "룩북" in source.templates

    with pytest.raises(StoreEditError, match="none.jpg"):
        store_edit.apply_detail(Services(db, client), NO, source, "깨짐")
    assert not client.puts and not list(store_edit.backup_dir().glob("*.html"))   # 올리기 전에 멈춤

    store_edit.apply_detail(Services(db, client), NO, source, "룩북")
    content = client.puts[0]["originProduct"]["detailContent"]
    assert '"images/' not in content and re.search(r'<img src="https://shop-phinf/new[^"]*"', content)


def test_apply_detail_refuses_template_with_style_block(env):
    """<style>로 꾸민 템플릿은 스토어에서 글자만 남으므로 올리기 전에 막는다 (백업·업로드·수정 없음)."""
    db, _, excel = env
    setup_item(env)
    folder = excel.parent / detail_page.USER_TEMPLATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "스타일.html").write_text('<style>.a{color:red}</style><div class="a">{{ p.name }}</div>', encoding="utf-8")
    client = StoreClient(store_raw())
    source = store_edit.load_detail_source(db, NO, store_raw())
    with pytest.raises(StoreEditError, match="style"):
        store_edit.apply_detail(Services(db, client), NO, source, "스타일")
    assert not client.puts and not list(store_edit.backup_dir().glob("*.html"))


def test_delete_user_template_moves_file_and_keeps_builtins(env):
    db, _, excel = env
    settings = load_settings(db)
    folder = excel.parent / detail_page.USER_TEMPLATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "내꺼.html").write_text("<div>{{ p.name }}</div>", encoding="utf-8")
    (folder / "기본.html").write_text("<div>내 기본</div>", encoding="utf-8")   # 내장 '기본'을 덮어쓴 파일

    assert not store_edit.template_info(settings, "심플").deletable          # 앱 내장
    with pytest.raises(StoreEditError, match="지울 수 없습니다"):
        store_edit.delete_user_template(settings, "심플")

    moved = store_edit.delete_user_template(settings, "내꺼")
    assert not (folder / "내꺼.html").exists() and moved.parent.name == "삭제한_템플릿"
    assert moved.read_text(encoding="utf-8") == "<div>{{ p.name }}</div>"     # 지우지 않고 옮김 (되살릴 수 있음)
    assert "내꺼" not in store_edit.available_templates(settings)
    assert "삭제한_템플릿" not in str(store_edit.available_templates(settings))

    info = store_edit.template_info(settings, "기본")
    assert info.deletable and info.builtin
    store_edit.delete_user_template(settings, "기본")
    assert "기본" in store_edit.available_templates(settings)                  # 내장 '기본'으로 돌아감
    assert not store_edit.template_info(settings, "기본").deletable


def test_mappings_using_template(env):
    from services.automation.config import CategoryMapping, save_mappings
    db, _, _ = env
    save_mappings(db, [CategoryMapping(name="니트", category_id="1", detail_template="에디토리얼"),
                       CategoryMapping(name="후드", category_id="2", detail_template="기본")])
    assert store_edit.mappings_using_template(db, "에디토리얼") == ["니트"]
    assert store_edit.mappings_using_template(db, "없음") == []


# --- 상세페이지에 넣을 사진 바꾸기 (상품 사진 · 착용 컷 · 디테일 컷 · 긴 상세 이미지) ---
def page_setup(env):
    """상품 사진 4장 + detail 사진 2장(보통 비율 1, 긴 이미지 1)."""
    db, root, excel = env
    folder = root / "img" / "A001" / "원본"
    folder.mkdir(parents=True, exist_ok=True)
    for n in ("01.png", "02.png", "03.png", "04.png"):
        Image.new("RGB", (600, 600), (100, 120, 200)).save(folder / n)
    Image.new("RGB", (600, 800), (10, 20, 30)).save(folder / "90_detail_cut.png")
    Image.new("RGB", (430, 1000), (10, 20, 30)).save(folder / "91_detail_long.png")
    add_rows(excel, [row("A001", 상태="판매중", 실측="S:어깨40", 소재="면 100%", **{"설명(최종)": "좋은 옷"})])
    db.upsert_automation_item("A001", "판매중", origin_product_no=NO)
    return store_edit.load_detail_source(db, NO, store_raw())


def test_folder_page_photos_split_into_four_groups(env):
    source = page_setup(env)
    page = source.folder_page()
    names = lambda group: [p.stem for p in group]   # noqa: E731
    assert names(page.photos) == ["01", "02", "03", "04"]
    assert names(page.cuts) == ["90_detail_cut"] and names(page.pages) == ["91_detail_long"]
    assert names(page.views) == ["03", "04"]                                 # 대표·커버 다음부터


def test_page_photos_edit_groups_without_touching_folder(env, tmp_path):
    db, root, _ = env
    source = page_setup(env)
    folder_before = sorted(p.name for p in (root / "img" / "A001").rglob("*"))
    page = source.folder_page()
    new = tmp_path / "새 넥라인.png"
    Image.new("RGB", (2000, 1200), (200, 190, 170)).save(new)
    # 디테일 컷: 04를 빼고 새 사진 추가, 착용 컷의 사진을 디테일 컷으로 옮김
    edited = detail_page.PagePhotos(photos=page.photos, cuts=[], views=[page.views[0], new, page.cuts[0]],
                                    pages=page.pages)
    store_edit.set_page_photos(source, edited)
    assert store_edit.page_photos_changed(source)

    client = StoreClient(store_raw())
    store_edit.apply_detail(Services(db, client), NO, source, "심플")
    converted = next(iter(source._converted.values()))
    assert converted.suffix == ".jpg" and "상세수정" in str(converted)
    with Image.open(converted) as im:
        assert im.size == (1000, 600)                                         # 가로 1000px, 비율 유지
    assert converted.name in client.uploads and len(client.uploads) == len(set(client.uploads))
    assert sorted(p.name for p in (root / "img" / "A001").rglob("*")) == folder_before   # 사진 폴더 그대로

    resolved = store_edit.resolved_page_photos(source)
    ctx = detail_page.build_context(source.prepared.draft, source.prepared.item, source.prepared.images, None,
                                    lambda p: Path(p).stem, page_photos=resolved)
    assert ctx["detail_cuts"] == [] and ctx["detail_views"][0] == "03" and ctx["detail_views"][2] == "90_detail_cut"

    store_edit.set_page_photos(source, detail_page.PagePhotos(photos=[], views=page.views))
    with pytest.raises(StoreEditError, match="1장 이상"):
        store_edit.preview_detail(source, "심플")
    store_edit.set_page_photos(source, source.folder_page())                  # 기본 구성과 같으면 '바꾸지 않음'
    assert not store_edit.page_photos_changed(source)


def test_editorial_like_template_uses_cuts_and_views(env):
    """착용 컷은 포인트 위, 디테일 컷은 '자세히 보기'에 들어간다 (템플릿 변수 확인)."""
    db, _, excel = env
    source = page_setup(env)
    folder = excel.parent / detail_page.USER_TEMPLATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "자리.html").write_text(
        "{% for s in p.detail_cuts %}[컷:{{ s }}]{% endfor %}{% for s in p.detail_views %}[뷰:{{ s }}]{% endfor %}",
        encoding="utf-8")
    page = source.folder_page()
    store_edit.set_page_photos(source, detail_page.PagePhotos(photos=page.photos, cuts=[page.photos[3]],
                                                              views=[page.cuts[0]], pages=[]))
    html = store_edit.render_detail(source, "자리", lambda p: Path(p).stem)
    assert html == "[컷:04][뷰:90_detail_cut]"


def test_detail_tab_photo_group_tabs(env, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui import store_edit_tabs as tabs
    app = QApplication.instance() or QApplication([])  # noqa: F841
    db, _, _ = env
    page_setup(env)
    monkeypatch.setattr(tabs, "run_async", lambda fn, done, fail: done(fn()))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    client = StoreClient(store_raw())
    tab = tabs.DetailTab(Services(db, client), NO, store_raw())
    tab.ensure_loaded()
    titles = [tab.photo_tabs.tabText(i) for i in range(tab.photo_tabs.count())]
    assert titles == ["상품 사진 (4)", "착용 컷 (1)", "디테일 컷 (2)", "긴 상세 이미지 (1)"]
    lists = tab.page_lists
    assert [lists["photos"].item(i).text() for i in range(3)] == ["대표", "커버", "사진 3"]
    assert lists["views"].item(0).text() == "디테일 1"

    tab.photo_tabs.setCurrentIndex(1)                                         # 착용 컷 → 디테일 컷으로 옮기기
    lists["cuts"].setCurrentRow(0)
    tab.move_page_photos("views")
    assert lists["cuts"].count() == 0 and lists["views"].count() == 3
    tab.photo_tabs.setCurrentIndex(2)
    new = tmp_path / "추가.png"
    Image.new("RGB", (500, 500)).save(new)
    tab.active_list().add_files([new, tmp_path / "x.txt"])
    assert lists["views"].item(3).text() == "디테일 4 · 새 사진"
    assert tab.photo_tabs.tabText(2) == "디테일 컷 (4)"

    tab.on_apply()
    assert len(client.puts) == 1
    tab.photo_reset_btn.click()
    assert lists["views"].count() == 2 and lists["cuts"].count() == 1
    assert not store_edit.page_photos_changed(tab.source)
