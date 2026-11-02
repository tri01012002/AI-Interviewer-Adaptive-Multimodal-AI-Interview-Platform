"""
Chạy indexing cho các file .md đã bóc từ PDF.

Cấu trúc thư mục (giống build_all_contexts): <root>/<doc_type>/*.md
  doc_type = tên thư mục: cv | jd | company | policy | knowledge
Bỏ qua file bắt đầu bằng 'context_' (output của context_builder).
Kết quả: <out>/<doc_type>/<tên file>.json

  python run_indexing.py [root] [--out DIR]
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import argparse
import sys


HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agents.rag_agent.chunking_agent import ChunkingConfig, index_file
from core.llm_adapter import make_llm_call
from core.state_indexing import DOC_TYPES

from core.llm import LLMClient
from core.configs import env_config
from prompts.indexing.chunking_prompts import build_system_prompt, build_user_prompt
from rag.chunking.context_builder import build_numbered_lines, split_into_parts

# path = r"D:\PHUOC\RAI_LLM\RAT_INTERVIEW\AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform\rag\documents\data_parser\cv\context_CV_ĐẶNG_QUỐC_BẢO.md"
# print(env_config.llm_provider, env_config.model)
# text = Path(path).read_text(encoding="utf-8")
# part = split_into_parts(build_numbered_lines(text), raw_lines=text.splitlines())[0]
# msgs = [("system", build_system_prompt("cv")), ("human", build_user_prompt("cv", "x", 0, 1, part.render()))]
# r = LLMClient()._llm.bind(max_tokens=6000, temperature=0).invoke(msgs)
# print(r.response_metadata)
# print(r.content[:500])


def main():
    default_root = HERE.parent / "documents" / "data_parser"
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=str(default_root))
    ap.add_argument("--out", default=None, help="Thư mục kết quả (mặc định <root>/../indexed)")
    args = ap.parse_args()

    root = Path(args.root)
    out_root = Path(args.out) if args.out else root.parent / "indexed"
    llm_call = make_llm_call()
    cfg = ChunkingConfig()

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        doc_type = folder.name.lower()
        if doc_type not in DOC_TYPES:
            print(f"[SKIP] thư mục '{folder.name}' không phải doc_type hợp lệ {DOC_TYPES}")
            continue
        if doc_type != "cv":      # <-- thêm dòng này (tạm thời, khi test)
            continue
        for md in sorted(folder.glob("*.md")):
            if md.name.startswith("context_"):
                continue
            try:
                doc = index_file(str(md), doc_type, llm_call, cfg)
            except Exception as e:
                print(f"[ERR] {doc_type}/{md.name}: {e}")
                continue
            out = out_root / doc_type / f"{md.stem}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
            print(f"[OK] {doc_type}/{md.name}: {len(doc.chunks)} chunk, {len(doc.warnings)} cảnh báo -> {out}")


if __name__ == "__main__":
    main()
