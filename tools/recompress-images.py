#!/usr/bin/env python
"""Happy-LLM 图片有损重压缩：PNG/JPEG -> WebP q85，超宽缩放到 <=2400px。

幂等：已是 WebP 或压缩后不更小的文件保持原样。
处理后同步更新 src/assets/manifest.json（file/mime/dataUriPrefix/sha256），
资源 ID 保持不变（内容文件中的 @asset/<id> 引用无需改动）。

用法: py tools/recompress-images.py [--dry-run]
"""
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "src" / "assets" / "manifest.json"
SCOPE_PREFIX = "src/assets/happy-llm/"
MAX_DIM = 2400
QUALITY = 85
WEBP_PREFIX = "data:image/webp;base64,"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    total_before = total_after = 0
    converted = skipped = 0
    deletions = []

    for asset_id, entry in manifest.items():
        if not entry["file"].startswith(SCOPE_PREFIX):
            continue
        if entry["mime"] not in ("image/png", "image/jpeg"):
            skipped += 1  # 已是 webp/svg 等
            continue

        src = ROOT / entry["file"]
        before = src.stat().st_size
        total_before += before

        img = Image.open(src)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        if max(img.size) > MAX_DIM:
            scale = MAX_DIM / max(img.size)
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)

        dst = src.with_suffix(".webp")
        img.save(dst, "WEBP", quality=QUALITY, method=6)
        after = dst.stat().st_size

        if after >= before:
            dst.unlink()
            total_after += before
            skipped += 1
            print(f"[skip] {asset_id}  webp 不更小 ({before} -> {after})")
            continue

        entry["file"] = str(dst.relative_to(ROOT)).replace("\\", "/")
        entry["mime"] = "image/webp"
        entry["dataUriPrefix"] = WEBP_PREFIX
        entry["sha256"] = hashlib.sha256(dst.read_bytes()).hexdigest()
        deletions.append(src)
        total_after += after
        converted += 1
        print(f"[webp] {asset_id}  {before/1048576:.2f}MB -> {after/1048576:.2f}MB")

    print("---")
    print(f"转换 {converted} 个，跳过 {skipped} 个；scope 内 {total_before/1048576:.1f}MB -> {total_after/1048576:.1f}MB")
    if dry_run:
        print("dry-run：未写盘")
        for d in deletions:  # deletions 存的是原图路径；dry-run 只清理由此产生的 .webp，不动原图
            d.with_suffix(".webp").unlink(missing_ok=True)
        return
    if converted:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for old in deletions:
            old.unlink()
        print(f"manifest 已更新，删除原图 {len(deletions)} 个")


if __name__ == "__main__":
    main()
