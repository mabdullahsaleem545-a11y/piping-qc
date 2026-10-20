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
# HEADING kept for reference
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
    line = line.replace(" ", " ").replace("‐", "-").replace("‑", "-").replace("–", "-").replace("’", "'")
    line = re.sub(r"_{3,}", " ", line)
    return re.sub(r"[ \t]+", " ", line).strip()


NOISE = re.compile(
    r"^(document responsibility|issue date|next planned update|previous issue|saudi aramco: company general use|"
    r"revised paragraphs are indicated|contact:|copyright|©|engineering (standard|procedure)$|page \d+ of \d+$|contents$|"
    r"saudi aramco desktop standards|this document is|developed by)", re.I)


def is_noise(line, doc):
    if len(line) < 2 or re.fullmatch(r"(page\s*)?\d+(\s*of\s*\d+)?", line, re.I):
        return True
    if NOISE.search(line) or re.search(r"\.{5,}\s*\d*$", line):  # page furniture, contents leaders
        return True
    # "SAEP-351 11 January 2017" cover line
    return doc.lower() in line.lower() and re.search(r"\d{4}\s*$", line) is not None and len(line) < len(doc) + 25


NUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,5})(\.?)\s+(\S.*)$")
APPX = re.compile(r"^(Appendix\s+[A-Z0-9]{1,3})\s*[-:–]?\s*(.*)$", re.I)
SKIP_SECTIONS = re.compile(r"^(references|applicable documents|revision summary|contents)$", re.I)


def is_title(text):
    """Short title-like text: 'Scope', 'Tightening Sequence', 'Saudi Aramco References'."""
    return (len(text) <= 60 and not re.search(r"[.;:,]$", text) and text[:1].isupper() and len(text.split()) <= 9
            and not re.search(r"\b(shall|should|must|may|is|are|be|will|can|not)\b", text, re.I))


def split_doc(doc, pages):
    """Split page texts into clause chunks {c: clause no., h: section title, p: page, t: text}.
    Every numbered paragraph starts a clause; the heading is the nearest section title."""
    clauses, cur = [], None
    title, sub, skipping, last_top, in_appx = "Introduction", None, False, 0, False

    def start(num, pno, text):
        nonlocal cur
        cur = {"c": num, "h": sub or title, "p": pno, "lines": [text] if text else []}
        clauses.append(cur)

    in_toc = False
    for pno, page in enumerate(pages, 1):
        raw_lines = [clean(r) for r in page.splitlines()]
        leaders = sum(1 for l in raw_lines if re.search(r"\.{2,}\s*\d+\s*$", l))
        if in_toc and leaders == 0:
            in_toc = False
        for line in raw_lines:
            if re.fullmatch(r"(table of )?contents", line, re.I):
                in_toc = True  # the contents list runs to the end of the page (and on, while leaders continue)
            if in_toc or is_noise(line, doc):
                continue
            m, a = NUM.match(line), APPX.match(line)
            top = m and "." not in m.group(1)
            ok = m and 0 < int(m.group(1).split(".")[0]) <= 40 and not re.match(r"^\d", m.group(3))
            first = int(m.group(1).split(".")[0]) if m else 0
            if a and (not a.group(2) or is_title(a.group(2))):
                title, sub, skipping, in_appx = (a.group(1) + (" - " + a.group(2) if a.group(2) else "")), None, False, True
                start(a.group(1), pno, "")
            elif ok and top and m.group(2) == "" and is_title(m.group(3)) and not in_appx and (first == last_top + 1 or (last_top == 0 and first <= 2)):
                # top-level sections run 1, 2, 3 ... ; numbered checklist items don't
                title, sub, last_top = m.group(3), None, first
                skipping = bool(SKIP_SECTIONS.match(title))
                if not skipping:
                    start(m.group(1), pno, "")
            elif skipping:
                continue
            elif ok and not top and not in_appx and first == last_top:
                if is_title(m.group(3)):
                    sub = m.group(3)
                    start(m.group(1), pno, "")
                else:
                    start(m.group(1), pno, m.group(3))
            elif cur is None:
                start("", pno, line)
            else:
                cur["lines"].append(line)

    # merge tiny neighbouring clauses under the same title, then cap length
    merged = []
    for cl in clauses:
        text = re.sub(r"\s+", " ", " ".join(cl["lines"])).strip()
        if not text:
            continue
        prev = merged[-1] if merged else None
        if prev and prev["h"] == cl["h"] and len(prev["t"]) + len(text) < 700 and cl["c"] and prev["c"]:
            prev["t"] += " (" + cl["c"] + ") " + text
            prev["c"] = prev["c"].split("-")[0] + "-" + cl["c"]
        else:
            merged.append({"c": cl["c"], "h": cl["h"], "p": cl["p"], "t": text})
    chunks = []
    for ch in merged:
        text = ch["t"]
        while text:
            piece = text[:MAX_CHUNK]
            if len(text) > MAX_CHUNK:
                cut = piece.rfind(". ")
                if cut > MAX_CHUNK * 0.5:
                    piece = piece[: cut + 1]
            chunks.append({**ch, "t": piece.strip()})
            text = text[len(piece):].strip()
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
    for page in pages[1:3]:
        m = re.search(r"Next Planned Update:\s*\d{1,2}\s+\w+,?\s+\d{4}\s+(.+)", page)
        if m and len(clean(m.group(1))) > 5:
            return clean(m.group(1))[:120]
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
