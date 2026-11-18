import os
import io
import json
import sqlite3
import tempfile
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect
import fitz  # PyMuPDF
from PIL import Image, ImageOps
import qrcode
import cv2
import numpy as np
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# Сүрөттөр браузерде кысылып келет, бирок серверде да 50 МБ чек коёбуз
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'beneficiaries.db')
TEMPLATE_ARMALA = os.path.join(BASE_DIR, 'template.pdf')
TEMPLATE_SIROTY = os.path.join(BASE_DIR, 'template_siroty.pdf')

GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID', '').strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()


def get_drive_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON орнотулган эмес!")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def upload_pdf_to_drive(file_path, filename):
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID орнотулган эмес!")

    service = get_drive_service()
    file_metadata = {
        'name': filename,
        'parents': [GOOGLE_DRIVE_FOLDER_ID]
    }
    media = MediaFileUpload(file_path, mimetype='application/pdf', resumable=False)
    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webContentLink, webViewLink'
    ).execute()

    file_id = created.get('id')
    # Шилтеме аркылуу кирген ар бир адам жүктөй алышы үчүн уруксат беребиз
    service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    return download_url


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS generated_pdfs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'armala',
            file_number TEXT NOT NULL,
            name TEXT NOT NULL,
            download_url TEXT NOT NULL,
            is_downloaded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


init_db()


def smart_crop_and_resize(file_storage, target_w, target_h, focus_face=False):
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img)
    if img.mode != 'RGB':
        img = img.convert('RGB')

    w, h = img.size
    target_ratio = target_w / target_h
    current_ratio = w / h

    center_x = w / 2
    center_y = h / 2

    if focus_face:
        try:
            cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
            face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            )
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
            if len(faces) > 0:
                fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                center_x = fx + fw / 2
                center_y = fy + fh * 0.45
        except Exception:
            pass

    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        new_h = h
        left = int(max(0, min(w - new_w, center_x - new_w / 2)))
        top = 0
    else:
        new_w = w
        new_h = int(w / target_ratio)
        left = 0
        top = int(max(0, min(h - new_h, center_y - new_h / 2)))

    img = img.crop((left, top, left + new_w, top + new_h))
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


def generate_qr_bytes(link_text):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(link_text.strip())
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    return buf.getvalue()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/pdfs', methods=['GET'])
def list_pdfs():
    category = request.args.get('category', 'armala').strip()
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 10))
    q = request.args.get('q', '').strip()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if q:
        c.execute(
            'SELECT COUNT(*) FROM generated_pdfs WHERE category = ? AND file_number LIKE ?',
            (category, f'%{q}%')
        )
        total = c.fetchone()[0]
        c.execute('''
            SELECT id, category, file_number, name, download_url, is_downloaded, created_at
            FROM generated_pdfs
            WHERE category = ? AND file_number LIKE ?
            ORDER BY is_downloaded ASC, id DESC
            LIMIT ? OFFSET ?
        ''', (category, f'%{q}%', limit, offset))
    else:
        c.execute('SELECT COUNT(*) FROM generated_pdfs WHERE category = ?', (category,))
        total = c.fetchone()[0]
        c.execute('''
            SELECT id, category, file_number, name, download_url, is_downloaded, created_at
            FROM generated_pdfs
            WHERE category = ?
            ORDER BY is_downloaded ASC, id DESC
            LIMIT ? OFFSET ?
        ''', (category, limit, offset))

    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    return jsonify({
        'ok': True,
        'items': rows,
        'total': total,
        'has_more': (offset + len(rows)) < total
    })


@app.route('/api/pdfs/<int:pdf_id>/downloaded', methods=['POST'])
def mark_downloaded(pdf_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE generated_pdfs SET is_downloaded = 1 WHERE id = ?', (pdf_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/pdfs/<int:pdf_id>', methods=['DELETE'])
def delete_pdf(pdf_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM generated_pdfs WHERE id = ?', (pdf_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/pdfs/bulk-delete', methods=['POST'])
def bulk_delete_pdfs():
    data = request.get_json(silent=True) or {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if 'delete_all_category' in data:
        c.execute('DELETE FROM generated_pdfs WHERE category = ?', (data['delete_all_category'],))
    elif 'ids' in data and isinstance(data['ids'], list) and data['ids']:
        placeholders = ','.join('?' for _ in data['ids'])
        c.execute(f'DELETE FROM generated_pdfs WHERE id IN ({placeholders})', data['ids'])
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/generate', methods=['POST'])
def generate_pdf():
    category = request.form.get('category', 'armala').strip()
    file_number = request.form.get('file_number', '').strip()
    link = request.form.get('link', '').strip()

    if not file_number:
        return jsonify({'ok': False, 'error': 'Файл номерин (رقم الملف) жазыңыз!'}), 400

    template_path = TEMPLATE_SIROTY if category == 'siroty' else TEMPLATE_ARMALA
    if not os.path.exists(template_path):
        return jsonify({'ok': False, 'error': f'Шаблон табылган жок: {os.path.basename(template_path)}'}), 500

    temp_pdf_path = None
    try:
        doc = fitz.open(template_path)
        page = doc[0]

        # 1. Жеке сүрөт (photo10)
        photo10 = request.files.get('photo10')
        if photo10 and photo10.filename:
            img_bytes = smart_crop_and_resize(photo10, 600, 900, focus_face=True)
            rect10 = fitz.Rect(72, 165, 222, 390)
            page.insert_image(rect10, stream=img_bytes)

        # 2. Кошумча 3 сүрөт (photo1, photo2, photo3)
        photo_slots = [
            ('photo1', fitz.Rect(55, 535, 215, 760)),
            ('photo2', fitz.Rect(220, 535, 380, 760)),
            ('photo3', fitz.Rect(385, 535, 545, 760)),
        ]
        for field_name, rect in photo_slots:
            f = request.files.get(field_name)
            if f and f.filename:
                img_bytes = smart_crop_and_resize(f, 1000, 1400, focus_face=False)
                page.insert_image(rect, stream=img_bytes)

        # 3. QR код
        if link:
            qr_bytes = generate_qr_bytes(link)
            qr_rect = fitz.Rect(465, 175, 545, 255)
            page.insert_image(qr_rect, stream=qr_bytes)

        prefix = 'siroty' if category == 'siroty' else 'armala'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        pdf_filename = f"{prefix}_{file_number}_{timestamp}.pdf"

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            temp_pdf_path = tmp.name
        doc.save(temp_pdf_path)
        doc.close()

        # Түз Google Drive папкасына жүктөйбүз
        download_url = upload_pdf_to_drive(temp_pdf_path, pdf_filename)

        # Сервердеги убактылуу файлды өчүрөбүз
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)
            temp_pdf_path = None

        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO generated_pdfs (category, file_number, name, download_url, is_downloaded, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        ''', (category, file_number, pdf_filename, download_url, created_at))
        conn.commit()
        conn.close()

        return jsonify({
            'ok': True,
            'file_number': file_number,
            'name': pdf_filename,
            'download': download_url
        })

    except Exception as e:
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
            except Exception:
                pass
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
