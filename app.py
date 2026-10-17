import io
import os
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime
import cv2
import fitz
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for

# ============================================================
# ПАПКАЛАР
# ============================================================
BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "template.pdf"
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS beneficiaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_number TEXT NOT NULL UNIQUE,
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
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

init_db()

# ============================================================
# АРИП ЖАНА TIMES NEW ROMAN
# ============================================================
def _value_font(size_px):
    font_path = BASE / "NotoNaskhArabic-Regular.ttf"
    if font_path.exists():
        try:
            return ImageFont.truetype(str(font_path), size_px)
        except Exception:
            pass

    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = os.path.join(windir, "Fonts")
    candidates = [
        os.path.join(fonts_dir, "times.ttf"),
        os.path.join(fonts_dir, "timesbd.ttf"),
        os.path.join(fonts_dir, "Times New Roman.ttf"),
        os.path.join(fonts_dir, "timesnewroman.ttf"),
        "times.ttf",
        "Times New Roman.ttf",
        "timesnewroman.ttf",
        os.path.join(fonts_dir, "arial.ttf"),
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size_px)
        except Exception:
            continue
    return ImageFont.load_default()

def _has_arabic(text):
    return any(
        "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" or "\u08a0" <= ch <= "\u08ff"
        for ch in str(text)
    )

def _format_text(text):
    text = str(text or "")
    if not text:
        return ""
    if _has_arabic(text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text

# ============================================================
# PDF FIELD NAMES (Бардык тексттик талаалар)
# ============================================================
TEXT = {
    "sponsor": (0, "Text1"),
    "beneficiary": (0, "Text2"),
    "beneficiary_number": (0, "Text3"),
    "internal_number": (1, "Text4"),
    "birth_date": (1, "Date2"),
    "gender": (1, "Dropdown3"),
    "health": (1, "Dropdown4"),
    "class_name": (1, "Text9"),
    "future_wish": (1, "Text5"),
    "impact": (1, "Text6"),
    "notes": (1, "Text7"),
}

# ============================================================
# АВТОМАТТЫК МААЛЫМАТТАР
# ============================================================
COUNTRY_VALUE = "قيرغيزستان"
YEAR_VALUE = "2027"
YEAR_FIELD = "Dropdown6_year"

# ============================================================
# СҮРӨТ ЖАНА ШИЛТЕМЕ FIELD'ТЕРИ
# ============================================================
IMAGES = {
    "photo10": (0, "Image1"),
    "photo1": (2, "Image3"),
    "photo2": (2, "Image2"),
    "photo3": (2, "Image4"),
    "qr_code": (2, "Image5"),
    "link_area": (2, "Text8"),
}

# ============================================================
# ТЕКСТТИ PDF ТАЛААСЫНА СҮРӨТ КАТАРЫ КОЮУ
# ============================================================
def _render_value(text, width_pt, height_pt, size_pt=18, align="right", top_align=False):
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None

    scale = 4
    W = max(40, int(width_pt * scale))
    H = max(30, int(height_pt * scale))
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)
    
    font = _value_font(max(10, int(size_pt * scale)))
    padding_x = 12 * scale
    max_width = W - (padding_x * 2)

    words = text.split()
    lines = []
    if len(words) <= 1:
        lines = [text]
    else:
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            try:
                box = draw.textbbox((0, 0), _format_text(candidate), font=font)
                line_width = box[2] - box[0]
            except Exception:
                line_width = len(candidate) * size_pt
            if line_width <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

    sample = _format_text("ابتAg0123456789")
    try:
        bbox = draw.textbbox((0, 0), sample, font=font)
        line_h = max(1, bbox[3] - bbox[1])
    except Exception:
        line_h = int(size_pt * scale)
    bbox = (0, 0, 0, line_h)

    spacing = max(2, int(size_pt * 0.20 * scale))
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing

    if top_align:
        y = 5 * scale
    else:
        y = max(0, int((H - total_h) / 2 - bbox[1]))

    for line in lines:
        processed_line = _format_text(line)
        try:
            line_box = draw.textbbox((0, 0), processed_line, font=font)
            line_w = line_box[2] - line_box[0]
        except Exception:
            line_w = len(processed_line) * (size_pt * scale * 0.5)

        if align == "right":
            x = W - padding_x - line_w
        elif align == "left":
            x = padding_x
        else:
            x = int((W - line_w) / 2)

        draw.text((x, y), processed_line, font=font, fill=(20, 20, 20, 255))
        y += line_h + spacing

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()

def put_text(page, field, value, size=18, align="right", top_align=False):
    try:
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                if value:
                    png = _render_value(
                        value, rect.width, rect.height,
                        size_pt=size, align=align, top_align=top_align
                    )
                    if png:
                        page.insert_image(rect, stream=png, keep_proportion=False, overlay=True)
                return True
    except Exception:
        pass
    return False

def put_automatic_fields(doc):
    try:
        put_text(doc[0], "Dropdown2", COUNTRY_VALUE, size=18, align="right")
        put_text(doc[0], YEAR_FIELD, YEAR_VALUE, size=18, align="right")
    except Exception:
        pass

# ============================================================
# FACE DETECTOR
# ============================================================
cascade_path = (cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
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

            crop = original.crop((int(left), int(top), int(left + desired_w), int(top + desired_h)))
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

        final = ImageOps.fit(crop, (800, 1200), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
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
# ГЕНЕРАЦИЯ QR КОДА
# ============================================================
def generate_qr_bytes(text):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
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

# ============================================================
# ШИЛТЕМЕ
# ============================================================
def put_link_text(page, field, link_text):
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

        scale = 5
        W = max(300, int(rect.width * scale))
        H = max(60, int(rect.height * scale))
        canvas = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        draw = ImageDraw.Draw(canvas)
        padding_x = 8 * scale
        max_width = W - padding_x * 2

        selected_font = _value_font(8 * scale)
        for size_pt in range(13, 5, -1):
            font = _value_font(size_pt * scale)
            bbox = draw.textbbox((0, 0), link_text, font=font)
            if bbox[2] - bbox[0] <= max_width:
                selected_font = font
                break

        bbox = draw.textbbox((0, 0), link_text, font=selected_font)
        text_w = bbox[2] - bbox[0]
        x = max(padding_x, min((W - text_w) // 2, W - text_w - padding_x))
        y = max(0, int((H - (bbox[3] - bbox[1])) / 2 - bbox[1]))

        draw.text((x, y), link_text, font=selected_font, fill=(20, 20, 20, 255))
        b = io.BytesIO()
        canvas.save(b, "PNG")
        page.insert_image(rect, stream=b.getvalue(), keep_proportion=False, overlay=True)

        link_uri = link_text.strip()
        if not link_uri.startswith(("http://", "https://")):
            link_uri = "https://" + link_uri
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": link_uri})
        return True
    except Exception:
        return False

# ============================================================
# PDF BUILD
# ============================================================
def build(form, files):
    if not TEMPLATE.exists():
        raise FileNotFoundError("template.pdf табылган жок.")

    doc = fitz.open(str(TEMPLATE))
    form_dict = dict(form)

    if not (form_dict.get("class_name") or "").strip():
        form_dict["class_name"] = (form_dict.get("class") or "").strip()

    for k, (p, f) in TEXT.items():
        value = (form_dict.get(k) or "").strip()
        top_align = k in ("impact", "notes")
        size = 18
        if k in ("impact", "notes"):
            size = 15
        elif k == "future_wish":
            size = 17

        if p < len(doc):
            put_text(doc[p], f, value, size=size, align="right", top_align=top_align)

    put_automatic_fields(doc)

    for k, (p, f) in IMAGES.items():
        if k in ("qr_code", "link_area"):
            continue
        if k in files and p < len(doc):
            put_image(doc[p], f, files[k], photo10=(k == "photo10"))

    link = (form_dict.get("link") or "").strip()
    if link and 2 < len(doc):
        put_qr_code(doc[2], IMAGES["qr_code"][1], link)

    file_number = (form_dict.get("file_number") or "NO_NUMBER").strip()
    safe_file_number = "".join(c for c in file_number if c.isalnum() or c in ("-", "_"))
    if not safe_file_number:
        safe_file_number = "NO_NUMBER"

    filename = (
        f"PDF_{safe_file_number}_"
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
def save_beneficiary(form_dict, pdf_filename):
    file_number = (form_dict.get("file_number") or "").strip()
    if not file_number:
        raise ValueError("Файл № киргизилиши керек.")

    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT id FROM beneficiaries WHERE file_number = ?", (file_number,)
        ).fetchone()

        if exists:
            conn.execute("""
                UPDATE beneficiaries SET
                    sponsor = ?, beneficiary = ?, beneficiary_number = ?, internal_number = ?,
                    birth_date = ?, gender = ?, health = ?, class_name = ?, future_wish = ?,
                    impact = ?, notes = ?, link = ?, pdf_filename = ?, created_at = ?
                WHERE file_number = ?
            """, (
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
                datetime.now().isoformat(timespec="seconds"),
                file_number,
            ))
        else:
            conn.execute("""
                INSERT INTO beneficiaries (
                    file_number, sponsor, beneficiary, beneficiary_number, internal_number,
                    birth_date, gender, health, class_name, future_wish, impact, notes,
                    link, pdf_filename, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_number,
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
                datetime.now().isoformat(timespec="seconds"),
            ))
        conn.commit()
    finally:
        conn.close()

# ============================================================
# ROUTES
# ============================================================
@app.get("/")
def home():
    pdfs = sorted([p.name for p in GENERATED.glob("*.pdf")], reverse=True)
    return render_template("index.html", pdfs=pdfs)

@app.post("/api/generate")
def generate():
    try:
        files = {}
        for k in ("photo10", "photo1", "photo2", "photo3"):
            f = request.files.get(k)
            if f and f.filename:
                files[k] = f.read()

        form_dict = dict(request.form)
        if not (form_dict.get("class_name") or "").strip():
            form_dict["class_name"] = (form_dict.get("class") or "").strip()

        file_number = (form_dict.get("file_number") or "").strip()
        if not file_number:
            return jsonify(ok=False, error="Файл № киргизиңиз."), 400

        out = build(request.form, files)
        try:
            save_beneficiary(form_dict, out.name)
        except Exception as e:
            out.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(
            ok=True,
            download=url_for("download", filename=out.name),
            file_number=file_number,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/api/search")
def search():
    file_number = (request.args.get("file_number") or "").strip()
    if not file_number:
        return jsonify(ok=False, error="Файл № жазыңыз."), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM beneficiaries WHERE file_number = ?", (file_number,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify(ok=False, error=f"Файл № {file_number} табылган жок."), 404

    data = dict(row)
    if data.get("pdf_filename"):
        data["pdf_url"] = url_for("download", filename=data["pdf_filename"])

    return jsonify(ok=True, beneficiary=data)

@app.get("/download/<path:filename>")
def download(filename):
    return send_from_directory(GENERATED, filename, as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
