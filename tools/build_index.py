#!/usr/bin/env python3
"""Build the encrypted knowledge index for the app's Ask tab.

Reads every PDF inside the given zip files or folders, splits the text into
clauses, and writes:

  <out>/manifest.json  public: version, build date, counts (no document text)
  <out>/index.bin      AES-256-GCM encrypted, gzip-compressed JSON

The passphrase comes from the INDEX_PASSPHRASE environment variable. The app
derives the same key with PBKDF2-SHA256 and decrypts the file on the phone.

Usage: INDEX_PASSPHRASE=... python build_index.py --out kb SAES.zip SAEP.zip
"""
import argparse, gzip, hashlib, io, json, os, re, sys, time, zipfile
from datetime import datetime, timezone

from pypdf import PdfReader
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

MAGIC = b"PQCIDX1\n"
ITERATIONS = 250_000
MAX_CHUNK = 1400  # characters per clause piece

DOC_ID = re.compile(r"(SAES|SAEP|SAMSS|SAIC|SATIP|SATR|GI)[-_ ]?([A-Z]{0,2})[-_ ]?(\d{1,4}(?:\.\d+)?)", re.I)
HEADING = re.compile(r"^\s*((?:\d{1,2}\.){0,5}\d{1,2}\.?|Appendix\s+[A-Z0-9]+|APPENDIX\s+[A-Z0-9]+|Table\s+\d+[A-Z]?)\s{1,4}([A-Z][^\n]{2,90})$")


def doc_id_from(name, text):
    for source in (name, text[:3000]):
        m = DOC_ID.search(source)
        if m:
            kind, letter, num = m.group(1).upper(), m.group(2).upper(), m.group(3)
            return f"{kind}-{letter}-{num}" if letter else f"{kind}-{num}"
    return os.path.splitext(os.path.basename(name))[0]


def iter_pdfs(paths):
    """Yield (name, bytes) for every PDF in the given zips, folders or files."""
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    full = os.path.join(root, f)
                    if f.lower().endswith(".pdf"):
                        yield f, open(full, "rb").read()
                    elif f.lower().endswith(".zip"):
                        yield from iter_zip(full)
        elif p.lower().endswith(".zip"):
            yield from iter_zip(p)
        elif p.lower().endswith(".pdf"):
            yield os.path.basename(p), open(p, "rb").read()


def iter_zip(path):
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.filename.lower().endswith(".pdf") and not info.filename.startswith("__MACOSX"):
                yield os.path.basename(info.filename), z.read(info)
            elif info.filename.lower().endswith(".zip"):
                inner = io.BytesIO(z.read(info))
                tmp = path + ".inner.zip"
                with open(tmp, "wb") as fh:
                    fh.write(inner.getvalue())
                yield from iter_zip(tmp)
                os.remove(tmp)


def clean(line):
    line = line.replace(" ", " ").replace("‐", "-").replace("‑", "-")
    return re.sub(r"[ \t]+", " ", line).strip()


def is_noise(line, doc):
    low = line.lower()
    return (not line or re.fullmatch(r"(page\s*)?\d+(\s*of\s*\d+)?", low) is not None
            or "saudi aramco: company general use" in low or low.startswith("document responsibility")
            or (doc.lower() in low and len(line) < len(doc) + 40 and re.search(r"issue date|next planned|page \d", low) is not None))


def split_doc(doc, pages):
    """Split page texts into clause chunks: {c: clause, h: heading, p: page, t: text}."""
    chunks, cur = [], {"c": "", "h": "Introduction", "p": 1, "lines": []}

    def flush():
        text = " ".join(cur["lines"]).strip()
        text = re.sub(r"\s+", " ", text)
        while text:
            piece = text[:MAX_CHUNK]
            if len(text) > MAX_CHUNK:
                cut = piece.rfind(". ")
                if cut > MAX_CHUNK * 0.5:
                    piece = piece[: cut + 1]
            chunks.append({"c": cur["c"], "h": cur["h"], "p": cur["p"], "t": piece.strip()})
            text = text[len(piece):].strip()

    for pno, page in enumerate(pages, 1):
        for raw in page.splitlines():
            line = clean(raw)
            if is_noise(line, doc):
                continue
            m = HEADING.match(line)
            if m and not re.search(r"\d{3,}\s*(psi|kpa|mm)", line, re.I):
                flush()
                cur = {"c": m.group(1).rstrip("."), "h": m.group(2).strip(), "p": pno, "lines": []}
            else:
                cur["lines"].append(line)
    flush()
    return [c for c in chunks if len(c["t"]) > 25]


def read_pdf(name, data):
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # damaged page: keep going
            pages.append("")
    return pages


def title_of(pages, doc):
    for line in "\n".join(pages[:1]).splitlines():
        line = clean(line)
        if len(line) > 12 and doc.lower() not in line.lower() and not re.search(r"saudi aramco|issue date|page|engineering standard|engineering procedure|document responsibility|^\d", line, re.I):
            return line[:120]
    return doc


def encrypt(payload, passphrase):
    salt, iv = os.urandom(16), os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    key = kdf.derive(passphrase.encode("utf-8"))
    return MAGIC + salt + iv + AESGCM(key).encrypt(iv, payload, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="zip files, PDFs or folders")
    ap.add_argument("--out", default="kb")
    args = ap.parse_args()
    passphrase = os.environ.get("INDEX_PASSPHRASE", "")
    if len(passphrase) < 10:
        sys.exit("Set INDEX_PASSPHRASE (at least 10 characters).")

    docs, chunks, seen, skipped = [], [], set(), []
    for name, data in iter_pdfs(args.inputs):
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            pages = read_pdf(name, data)
        except Exception as e:
            skipped.append(f"{name}: {e}")
            continue
        text = "\n".join(pages)
        if len(text.strip()) < 200:
            skipped.append(f"{name}: no text layer (scanned PDF?)")
            continue
        doc = doc_id_from(name, text)
        kind = "SAEP" if doc.startswith("SAEP") else "SAES" if doc.startswith("SAES") else doc.split("-")[0]
        di = len(docs)
        docs.append({"id": doc, "title": title_of(pages, doc), "kind": kind, "file": name, "pages": len(pages)})
        for ch in split_doc(doc, pages):
            ch["d"] = di
            chunks.append(ch)
        print(f"  {doc:<16} {len(pages):>4} pages", file=sys.stderr)

    if not chunks:
        sys.exit("No text found in any PDF.")
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = hashlib.sha1(("".join(sorted(seen)) + built).encode()).hexdigest()[:12]
    body = json.dumps({"v": version, "built": built, "docs": docs, "chunks": chunks}, ensure_ascii=False, separators=(",", ":")).encode()
    blob = encrypt(gzip.compress(body, 9), passphrase)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.bin"), "wb") as fh:
        fh.write(blob)
    manifest = {"version": version, "built": built, "documents": len(docs), "clauses": len(chunks), "bytes": len(blob),
                "saes": sum(d["kind"] == "SAES" for d in docs), "saep": sum(d["kind"] == "SAEP" for d in docs)}
    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(json.dumps(manifest), file=sys.stderr)
    for s in skipped:
        print("  skipped " + s, file=sys.stderr)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time() - t0:.1f}s", file=sys.stderr)
