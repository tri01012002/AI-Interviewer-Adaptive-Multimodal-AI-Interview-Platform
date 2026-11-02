import os
import re
import sys
from pathlib import Path
from docx2pdf import convert
from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions

# ==============================================================================
# 1. HÀM CHUYỂN ĐỔI FILE SANG PDF (NẾU CHƯA PHẢI PDF)
# ==============================================================================
def convert_to_pdf_if_needed(file_path: Path, temp_pdf_dir: Path) -> Path:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return file_path

    elif suffix in [".docx", ".doc"]:
        temp_pdf_dir.mkdir(parents=True, exist_ok=True)
        converted_pdf = temp_pdf_dir / f"{file_path.stem}.pdf"

        # Thử convert bằng docx2pdf nếu có MS Word
        try:
            from docx2pdf import convert
            if not converted_pdf.exists() or file_path.stat().st_mtime > converted_pdf.stat().st_mtime:
                print(f"   [CONVERT] Đang chuyển đổi: {file_path.name} -> {converted_pdf.name}...")
                convert(str(file_path), str(converted_pdf))
                print(f"   [CONVERT] Xong: {converted_pdf.name}")
            return converted_pdf
        except Exception as e:
            # Nếu máy không có Word, trả về chính file DOCX để Docling parse trực tiếp
            print(f"   ⚠️ Không thể convert sang PDF bằng MS Word ({e}). Docling sẽ đọc trực tiếp file DOCX.")
            return file_path
    else:
        return file_path


# ==============================================================================
# 2. KHỞI TẠO DOCLING ENGINE (TẮT OCR ĐỂ TỐI ƯU TỐC ĐỘ CPU)
# ==============================================================================


def get_optimized_docling_converter() -> DocumentConverter:
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True

    # Cho phép Docling nhận cả PDF lẫn DOCX mà không bị giới hạn chỉ mỗi PDF
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    return converter


def clean_filename(text: str) -> str:
    """Làm sạch tên file theo quy chuẩn hệ điều hành."""
    text = re.sub(r'[\\/*?:"<>|#\n\r]', "", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text[:80]


# ==============================================================================
# 3. QUY TẮC NHẬN DIỆN THẺ VÀ ĐẶT TÊN CHO TỪNG LOẠI THƯ MỤC CON
# ==============================================================================
CATEGORY_RULES = {
    "cv": {
        "tag_prefix": r"\[CV",
        "doc_type": "cv"
    },
    "jd": {
        "tag_prefix": r"\[JD",
        "doc_type": "jd"
    },
    "knowledge": {
        "tag_prefix": r"\[TÀI LIỆU",
        "doc_type": "knowledge"
    },
    "company": {
        "tag_prefix": r"\[DOC",
        "doc_type": "company"
    },
}


def split_and_save_chunks(markdown_text: str, category_name: str, output_folder: Path) -> list:
    """Cắt Markdown tổng thành các file Markdown con dựa trên tag và lưu vào thư mục đích."""
    output_folder.mkdir(parents=True, exist_ok=True)

    rule = CATEGORY_RULES.get(category_name.lower(), {
        "tag_prefix": rf"\[{category_name.upper()}",
        "doc_type": category_name.lower()
    })

    tag_prefix = rule["tag_prefix"]
    doc_type = rule["doc_type"]

    # Tách đoạn dựa trên regex
    split_pattern = rf"(?m)(?=^[#*\-\s]*{tag_prefix})"
    chunks = re.split(split_pattern, markdown_text)

    created_files = []
    index = 1

    for chunk in chunks:
        chunk = chunk.strip()
        if not re.search(tag_prefix, chunk, re.IGNORECASE):
            continue

        filename = ""

        # Quy tắc đặt tên cho từng loại
        if doc_type == "cv":
            name_match = re.search(r"\*\*(?:ỨNG VIÊN:?\s*)?([^*]+)\*\*", chunk)
            if not name_match:
                lines = [l.strip() for l in chunk.split("\n") if l.strip()]
                for l in lines[:7]:
                    l_clean = re.sub(r"^[#\s*]+", "", l).strip()
                    if re.search(r"^[A-ZÀ-Ỹ\s]{4,}$", l_clean) and "ỨNG TUYỂN" not in l_clean:
                        name_match = re.search(r"^([A-ZÀ-Ỹ\s]+)$", l_clean)
                        break

            if name_match:
                cand_name = clean_filename(name_match.group(1))
                filename = f"CV_{cand_name}.md"
            else:
                tag_match = re.search(r"\[CV\s*(\d+)\]", chunk[:120], re.I)
                num = tag_match.group(1) if tag_match else str(index)
                filename = f"CV_UNG_VIEN_{num}.md"

        elif doc_type == "jd":
            first_line = [l for l in chunk.split("\n") if l.strip()][0]
            clean_title = re.sub(r"^[#*\-\s]*\[JD\s*(\d+)\]\s*:?", r"JD_\1_", first_line, flags=re.I)
            clean_title = clean_filename(clean_title)
            filename = f"{clean_title}.md" if clean_title else f"JD_{index}.md"

        elif doc_type == "knowledge":
            first_line = [l for l in chunk.split("\n") if l.strip()][0]
            clean_title = re.sub(r"^[#*\-\s]*\[TÀI LIỆU\s*(\d+)\]\s*:?", r"KNOWLEDGE_\1_", first_line, flags=re.I)
            clean_title = clean_filename(clean_title)
            filename = f"{clean_title}.md" if clean_title else f"KNOWLEDGE_{index}.md"

        elif doc_type == "company":
            first_line = [l for l in chunk.split("\n") if l.strip()][0]
            clean_title = re.sub(r"^[#*\-\s]*\[DOC\s*(\d+)\]\s*:?", r"COMPANY_\1_", first_line, flags=re.I)
            clean_title = clean_filename(clean_title)
            filename = f"{clean_title}.md" if clean_title else f"COMPANY_{index}.md"

        else:
            filename = f"{category_name.upper()}_{index}.md"

        dest_file = output_folder / filename
        with open(dest_file, "w", encoding="utf-8") as f_out:
            f_out.write(chunk)

        created_files.append(filename)
        index += 1

    return created_files


# ==============================================================================
# 4. HÀM ĐIỀU PHỐI CHÍNH (MAIN PIPELINE VỚI BIẾN XÁC NHẬN)
# ==============================================================================
def process_data_origin_pipeline(
    data_origin_path: Path,
    data_parser_path: Path,
    is_confirmed: bool = True
) -> dict:
    """
    Tự động quét data_origin, convert sang PDF (nếu cần), 
    dùng Docling bóc tách và xuất file Markdown vào data_parser.
    """
    origin_dir = Path(data_origin_path).resolve()
    parser_dir = Path(data_parser_path).resolve()
    temp_pdf_dir = origin_dir / "_temp_pdf_cache"

    print("=" * 80)
    print(f"📂 INPUT (DATA_ORIGIN) : {origin_dir}")
    print(f"📁 OUTPUT (DATA_PARSER): {parser_dir}")
    print(f"🔘 XÁC NHẬN XỬ LÝ      : {'[TRUE]' if is_confirmed else '[FALSE - ĐANG CHỜ]'}")
    print("=" * 80)

    if not origin_dir.exists():
        print(f"❌ Không tìm thấy thư mục nguồn: {origin_dir}")
        return {"status": "error", "message": "Input directory not found"}

    # 1. Quét tìm tất cả các file trong các thư mục con
    supported_extensions = {".docx", ".doc", ".pdf"}
    files_to_process = []

    for sub_dir in origin_dir.iterdir():
        if sub_dir.is_dir() and not sub_dir.name.startswith("_"):
            for file_path in sub_dir.glob("*"):
                if file_path.suffix.lower() in supported_extensions:
                    files_to_process.append((sub_dir.name, file_path))

    print(f"\n📋 Đã tìm thấy {len(files_to_process)} file cần xử lý:")
    for cat, fpath in files_to_process:
        print(f"   • [{cat.upper()}] {fpath.name}")

    if not is_confirmed:
        print("\n⏸️ Chưa xác nhận xử lý (is_confirmed=False). Dừng pipeline.")
        return {"status": "waiting_confirmation", "files": [str(f[1]) for f in files_to_process]}

    # 2. Xử lý bóc tách khi is_confirmed == True
    print("\n🚀 Bắt đầu quá trình bóc tách sang data_parser...")
    converter = get_optimized_docling_converter()
    summary_report = {}
    grand_total_files = 0

    for category, file_path in files_to_process:
        print(f"\n────────────────────────────────────────────────────────────")
        print(f"🔄 Đang xử lý: [{category.upper()}] -> {file_path.name}")

        # Chuẩn hóa sang PDF (nếu có thể)
        pdf_or_docx_path = convert_to_pdf_if_needed(file_path, temp_pdf_dir)

        # Parse bằng Docling
        print(f"   [DOCLING] Bóc tách layout và bảng biểu...")
        docling_result = converter.convert(str(pdf_or_docx_path))
        markdown_text = docling_result.document.export_to_markdown()

        # Lưu vào: rag/documents/data_parser/<category>/
        target_output_dir = parser_dir / category
        created = split_and_save_chunks(markdown_text, category, target_output_dir)

        summary_report[category] = {
            "source_file": file_path.name,
            "output_folder": str(target_output_dir),
            "count": len(created),
            "sample_files": created[:3] if created else []
        }
        grand_total_files += len(created)
        print(f"   ✅ Đã tạo {len(created)} file vào: {target_output_dir}")

    print("\n" + "=" * 80)
    print(f"🎉 HOÀN TẤT! ĐÃ XUẤT {grand_total_files} FILES VÀO: {parser_dir}")
    print("=" * 80)

    return {"status": "success", "total_files": grand_total_files, "details": summary_report}


# ==============================================================================
# ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    # Đường dẫn tương đối tự động tìm theo vị trí file chạy
    BASE_DIR = Path(__file__).resolve().parent.parent  # Thư mục gốc project

    DATA_ORIGIN_DIR = BASE_DIR / "rag" / "documents" / "data_origin"
    DATA_PARSER_DIR = BASE_DIR / "rag" / "documents" / "data_parser"

    IS_CONFIRMED = True

    process_data_origin_pipeline(
        data_origin_path=DATA_ORIGIN_DIR,
        data_parser_path=DATA_PARSER_DIR,
        is_confirmed=IS_CONFIRMED
    )