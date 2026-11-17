import io
import os
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime

import cloudinary
import cloudinary.uploader
import cv2
import fitz
import numpy as np
import qrcode
from PIL import Image, ImageOps
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for

# ============================================================
# CLOUDINARY ЖӨНДӨӨЛӨРҮ
# ============================================================
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)


def upload_pdf_to_cloudinary(pdf_path, category="armala"):
    """PDF файлды Cloudinary булутуна оригиналдуу RAW PDF түрүндө жүктөйт"""
    if not os.environ.get("CLOUDINARY_CLOUD_NAME"):
        return None
    try:
        folder_name = f"as_safa_pdfs/{category}"
        res = cloudinary.uploader.upload(
            str(pdf_path),
            folder=folder_name,
            public_id=pdf_path.name,
            resource_type="raw",
            overwrite=True,
        )
        return res.get("secure_url")
    except Exception as e:
        print("Cloudinary жүктөө катасы:", e)
        return None


# ============================================================
# ПАПКАЛАР ЖАНА ШАБЛОНДОР
# ============================================================
BASE = Path(__file__).resolve().parent
TEMPLATE_ARMALA = BASE / "template.pdf"
TEMPLATE_SIROTY = BASE / "template_siroty.pdf"
GENERATED = BASE / "generated"
GENERATED.mkdir(exist_ok=True)
DATABASE = BASE / "beneficiaries.db"

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ============================================================
# SQLITE
# ============================================================
def get_db():
    conn = sqlite3.connect(str(DATABASE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beneficiaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_number TEXT NOT NULL,
                category TEXT DEFAULT 'armala',
                sponsor TEXT,
                beneficiary TEXT,
                beneficiary_number TEXT,
                internal_number TEXT,
                birth_date TEXT,
                gender TEXT,
                health TEXT,
                class_name TEXT,
                future_wish TEXT,
                impact TEXT,
                notes TEXT,
                link TEXT,
                pdf_filename TEXT,
                cloud_url TEXT,
                is_downloaded INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        for col_sql in [
            "ALTER TABLE beneficiaries ADD COLUMN cloud_url TEXT",
            "ALTER TABLE beneficiaries ADD COLUMN category TEXT DEFAULT 'armala'",
            "ALTER TABLE beneficiaries ADD COLUMN is_downloaded INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass

        conn.execute("UPDATE beneficiaries SET category = 'armala' WHERE category IS NULL OR category = ''")
        conn.execute("UPDATE beneficiaries SET is_downloaded = 0 WHERE is_downloaded IS NULL")
        conn.commit()
    finally:
        conn.close()


init_db()

# ============================================================
# PDF FIELD NAMES
# ============================================================
IMAGES = {
    "photo10": (0, "Image1"),
    "photo1": (2, "Image3"),
    "photo2": (2, "Image2"),
    "photo3": (2, "Image4"),
    "qr_code": (2, "Image5"),
}

# ============================================================
# FACE DETECTOR
# ============================================================
cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
if not os.path.exists(cascade_path):
    cascade_path = os.path.join(
        os.path.dirname(cv2.__file__), "data", "haarcascade_frontalface_default.xml"
    )
FACE_CASCADE = cv2.CascadeClassifier(cascade_path)


def prepare_photo10(data):
    try:
        original = Image.open(io.BytesIO(data)).convert("RGB")
        cv_image = cv2.cvtColor(np.array(original), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)

        faces = ()
        if not FACE_CASCADE.empty():
            faces = FACE_CASCADE.detectMultiScale(
                gray, scaleFactor=1.08, minNeighbors=5, minSize=(60, 60)
            )

        W, H = original.size
        target_ratio = 4 / 6

        if len(faces) > 0:
            faces_sorted = sorted(faces, key=lambda item: item[2] * item[3], reverse=True)
            x, y, fw, fh = faces_sorted[0]
            face_cx = x + fw / 2
            face_cy = y + fh / 2

            top_space = fh * 1.15
            bottom_space = fh * 2.35
            desired_h = top_space + fh + bottom_space
            desired_w = desired_h * target_ratio

            if desired_w > W:
                desired_w = W
                desired_h = desired_w / target_ratio
            if desired_h > H:
                desired_h = H
                desired_w = desired_h * target_ratio

            left = face_cx - desired_w / 2
            top = face_cy - fh * 0.95
            left = max(0, min(left, W - desired_w))
            top = max(0, min(top, H - desired_h))

            crop = original.crop(
                (int(left), int(top), int(left + desired_w), int(top + desired_h))
            )
        else:
            current_ratio = W / H
            if current_ratio > target_ratio:
                new_w = int(H * target_ratio)
                left = (W - new_w) // 2
                crop = original.crop((left, 0, left + new_w, H))
            else:
                new_h = int(W / target_ratio)
                top = (H - new_h) // 2
                crop = original.crop((0, top, W, top + new_h))

        final = ImageOps.fit(
            crop, (800, 1200), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
        out = io.BytesIO()
        final.save(out, "JPEG", quality=95, optimize=True)
        return out.getvalue()
    except Exception:
        return data


def put_image(page, field, data, photo10=False):
    try:
        rect = None
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                break
        if rect is None:
            return False

        if photo10:
            data = prepare_photo10(data)

        im = Image.open(io.BytesIO(data)).convert("RGB")
        im = ImageOps.fit(
            im,
            (max(300, int(rect.width * 6)), max(300, int(rect.height * 6))),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95)
        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True)
        page.insert_image(rect, stream=b.getvalue(), keep_proportion=False, overlay=True)
        return True
    except Exception:
        return False


# ============================================================
# QR КОД ЖАНА ШИЛТЕМЕ
# ============================================================
def generate_qr_bytes(text):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    b = io.BytesIO()
    img.save(b, "JPEG", quality=95)
    return b.getvalue()


def put_qr_code(page, field, link_text):
    try:
        rect = None
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                break
        if rect is None or not link_text:
            return False

        qr_bytes = generate_qr_bytes(link_text)
        im = Image.open(io.BytesIO(qr_bytes)).convert("RGB")
        im = ImageOps.fit(
            im,
            (max(300, int(rect.width * 6)), max(300, int(rect.height * 6))),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95)
        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True)
        page.insert_image(rect, stream=b.getvalue(), keep_proportion=False, overlay=True)
        return True
    except Exception:
        return False


def put_editable_link(page, field_name, link_text):
    try:
        for w in list(page.widgets() or []):
            if w.field_name == field_name:
                w.field_value = link_text or ""
                w.update()
                return True
    except Exception:
        pass
    return False


# ============================================================
# PDF BUILD
# ============================================================
def build(form, files, category="armala"):
    template_path = TEMPLATE_SIROTY if category == "siroty" else TEMPLATE_ARMALA
    if not template_path.exists():
        raise FileNotFoundError(f"Шаблон табылган жок: {template_path.name}")

    doc = fitz.open(str(template_path))
    form_dict = dict(form)

    for k, (p, f) in IMAGES.items():
        if k == "qr_code":
            continue
        if k in files and p < len(doc):
            put_image(doc[p], f, files[k], photo10=(k == "photo10"))

    link = (form_dict.get("link") or "").strip()
    if link and 2 < len(doc):
        put_qr_code(doc[2], IMAGES["qr_code"][1], link)
        put_editable_link(doc[2], "Text8", link)

    file_number = (form_dict.get("file_number") or "NO_NUMBER").strip()
    safe_file_number = "".join(
        c for c in file_number if c.isalnum() or c in ("-", "_")
    )
    if not safe_file_number:
        safe_file_number = "NO_NUMBER"

    prefix = "SIROTY" if category == "siroty" else "ARMALA"
    filename = (
        f"PDF_{prefix}_{safe_file_number}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:5]}.pdf"
    )
    out = GENERATED / filename
    doc.save(str(out), deflate=True)
    doc.close()
    return out


# ============================================================
# DATABASE SAVE
# ============================================================
def save_beneficiary(form_dict, pdf_filename, cloud_url=None, category="armala"):
    file_number = (form_dict.get("file_number") or "").strip()
    if not file_number:
        raise ValueError("Файл № киргизилиши керек.")

    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT id FROM beneficiaries WHERE file_number = ? AND category = ?",
            (file_number, category),
        ).fetchone()

        if exists:
            raise ValueError(f"Файл № {file_number} базада мурунтан бар! Кайра жасоо үчүн алгач тизмеден өчүрүңүз.")

        conn.execute(
            """
            INSERT INTO beneficiaries (
                file_number, category, sponsor, beneficiary, beneficiary_number, internal_number,
                birth_date, gender, health, class_name, future_wish, impact, notes,
                link, pdf_filename, cloud_url, is_downloaded, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                file_number,
                category,
                form_dict.get("sponsor", ""),
                form_dict.get("beneficiary", ""),
                form_dict.get("beneficiary_number", ""),
                form_dict.get("internal_number", ""),
                form_dict.get("birth_date", ""),
                form_dict.get("gender", ""),
                form_dict.get("health", ""),
                form_dict.get("class_name", ""),
                form_dict.get("future_wish", ""),
                form_dict.get("impact", ""),
                form_dict.get("notes", ""),
                form_dict.get("link", ""),
                pdf_filename,
                cloud_url,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# ROUTES
# ============================================================
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/pdfs")
def list_pdfs():
    """
    Категория боюнча 10 PDFтен кайтарат.
    Сорттоо: алгач жүктөлө электер (is_downloaded = 0) эң үстүндө,
    анан жүктөлгөндөр (is_downloaded = 1) астында турат.
    Издөө (q) берилсе Файл № боюнча издейт.
    """
    category = (request.args.get("category") or "armala").strip().lower()
    if category not in ("armala", "siroty"):
        category = "armala"

    query_text = (request.args.get("q") or "").strip()

    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = min(50, max(1, int(request.args.get("limit", 10))))
    except ValueError:
        offset, limit = 0, 10

    conn = get_db()

    if query_text:
        like_q = f"%{query_text}%"
        total = conn.execute(
            "SELECT COUNT(*) FROM beneficiaries WHERE category = ? AND file_number LIKE ?",
            (category, like_q),
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT id, file_number, beneficiary, pdf_filename, cloud_url, is_downloaded, created_at, category
            FROM beneficiaries
            WHERE category = ? AND file_number LIKE ?
            ORDER BY is_downloaded ASC, id DESC
            LIMIT ? OFFSET ?
            """,
            (category, like_q, limit, offset),
        ).fetchall()
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM beneficiaries WHERE category = ?", (category,)
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT id, file_number, beneficiary, pdf_filename, cloud_url, is_downloaded, created_at, category
            FROM beneficiaries
            WHERE category = ?
            ORDER BY is_downloaded ASC, id DESC
            LIMIT ? OFFSET ?
            """,
            (category, limit, offset),
        ).fetchall()

    conn.close()

    items = []
    for r in rows:
        url = r["cloud_url"] or (
            url_for("download", filename=r["pdf_filename"]) if r["pdf_filename"] else "#"
        )
        items.append({
            "id": r["id"],
            "file_number": r["file_number"],
            "category": r["category"],
            "beneficiary": r["beneficiary"] or "",
            "name": r["pdf_filename"] or f"PDF_{r['file_number']}.pdf",
            "url": url,
            "is_downloaded": bool(r["is_downloaded"]),
            "created_at": r["created_at"],
        })

    return jsonify(
        ok=True,
        items=items,
        total=total,
        has_more=(offset + len(items)) < total,
    )


@app.post("/api/pdfs/<int:item_id>/downloaded")
def mark_pdf_downloaded(item_id):
    """PDF бир жолу жүктөлгөнүн белгилейт (түсүн бозортуп, астына түшүрүү үчүн)"""
    conn = get_db()
    conn.execute("UPDATE beneficiaries SET is_downloaded = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.delete("/api/pdfs/<int:item_id>")
def delete_pdf_from_site(item_id):
    """Сайттын тизмесинен жана базадан өчүрөт (Cloudinary булутунан ӨЧПӨЙТ)"""
    conn = get_db()
    row = conn.execute(
        "SELECT pdf_filename FROM beneficiaries WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Табылган жок."), 404

    if row["pdf_filename"]:
        (GENERATED / row["pdf_filename"]).unlink(missing_ok=True)

    conn.execute("DELETE FROM beneficiaries WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.post("/api/generate")
def generate():
    try:
        files = {}
        for k in ("photo10", "photo1", "photo2", "photo3"):
            f = request.files.get(k)
            if f and f.filename:
                files[k] = f.read()

        form_dict = dict(request.form)
        category = (form_dict.get("category") or "armala").strip().lower()
        if category not in ("armala", "siroty"):
            category = "armala"

        file_number = (form_dict.get("file_number") or "").strip()
        if not file_number:
            return jsonify(ok=False, error="Файл № киргизиңиз."), 400

        # Файл № мурунтан бар-жогун PDF түзгөнгө чейин текшерүү
        conn = get_db()
        exists = conn.execute(
            "SELECT id FROM beneficiaries WHERE file_number = ? AND category = ?",
            (file_number, category),
        ).fetchone()
        conn.close()

        if exists:
            return jsonify(
                ok=False,
                error=f"Файл № {file_number} базада бар! Кайра жасоо үчүн алгач тизмеден өчүрүңүз (Удалить)."
            ), 400

        out = build(form_dict, files, category=category)
        cloud_url = upload_pdf_to_cloudinary(out, category=category)

        try:
            save_beneficiary(form_dict, out.name, cloud_url=cloud_url, category=category)
        except Exception as e:
            out.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(e)), 400

        if cloud_url:
            out.unlink(missing_ok=True)

        download_link = (
            cloud_url if cloud_url else url_for("download", filename=out.name)
        )
        return jsonify(
            ok=True,
            download=download_link,
            cloud_url=cloud_url,
            file_number=file_number,
            category=category,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.get("/download/<path:filename>")
def download(filename):
    return send_from_directory(GENERATED, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
