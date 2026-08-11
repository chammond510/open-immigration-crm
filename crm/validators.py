import os
import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _read_head(uploaded_file, length=4096):
    position = uploaded_file.tell()
    uploaded_file.seek(0)
    value = uploaded_file.read(length)
    uploaded_file.seek(position)
    return value


def _is_docx(uploaded_file):
    position = uploaded_file.tell()
    try:
        uploaded_file.seek(0)
        with zipfile.ZipFile(uploaded_file) as archive:
            names = set(archive.namelist())
            return {"[Content_Types].xml", "word/document.xml"}.issubset(names)
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    finally:
        uploaded_file.seek(position)


def validate_document(uploaded_file):
    extension = os.path.splitext((uploaded_file.name or "").lower())[1]
    if extension not in ALLOWED_DOCUMENT_TYPES:
        raise ValidationError("Allowed file types: PDF, DOCX, PNG, JPG, and JPEG.")

    max_bytes = settings.DOCUMENT_UPLOAD_MAX_BYTES
    if uploaded_file.size > max_bytes:
        raise ValidationError(f"The file exceeds the {max_bytes // (1024 * 1024)} MB limit.")

    head = _read_head(uploaded_file)
    detected = None
    if head.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif _is_docx(uploaded_file):
        detected = ALLOWED_DOCUMENT_TYPES[".docx"]

    if detected != ALLOWED_DOCUMENT_TYPES[extension]:
        raise ValidationError("The file contents do not match its extension.")
    return detected
