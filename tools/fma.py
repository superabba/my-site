"""Free Music Archive(Hugging Face 데이터셋)에서 영상 배경음악 후보를 골라 내려받는다.

데이터셋: benjamin-paine/free-music-archive-full (약 10만 곡, parquet 972개, 524GB)
전부 받을 수 없으므로
  scan   곡 정보 열만 읽어(오디오 제외) 상업 이용·편집 가능한 연주곡 목록을 만든다
  fetch  고른 곡의 mp3만 꺼낸다. 오디오는 100곡이 snappy로 압축된 페이지 하나에 들어 있어
         페이지 앞에서부터 그 곡까지만 받아 풀고 멈춘다 (그래서 페이지 앞쪽 곡일수록 빠르다)

라이선스: CC0·Public Domain Mark·CC BY(모든 버전)만 쓴다. 영상에 음악을 입히는 것은 CC에서 '2차적 저작물'이라
ND(변경 금지)·SA(동일조건)는 안 되고, NC(비영리)는 수익 계정에 못 쓴다. CC BY는 출처 표기가 필요하다.

필요: pip install pyarrow huggingface_hub  (이 도구만 씀, 게시 워크플로에는 필요 없음)
"""

from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DATASET = "datasets/benjamin-paine/free-music-archive-full/data"
N_FILES = 972
ALLOWED = {"CC0 1.0", "Public Domain Mark 1.0", "CC-BY 1.0", "CC-BY 2.0", "CC-BY 2.5", "CC-BY 3.0", "CC-BY 4.0"}
META_COLS = ["title", "url", "artist", "artist_url", "album_title", "genres", "tags", "listens", "license",
             "explicit", "instrumental", "allow_commercial_use", "allow_derivatives", "require_attribution",
             "require_share_alike"]

# 분위기(bgm.py의 이름)별로 어울리는 FMA 장르. 최종 선택은 사람이 들어 보고 한다
MOOD_GENRES = {
    "calm": ("Ambient", "Ambient Electronic", "Chill-out", "Downtempo", "Lo-Fi", "New Age", "Minimalism",
             "Chamber Music", "Classical", "Contemporary Classical", "Lounge"),
    "warm": ("Folk", "Americana", "Easy Listening", "Jazz", "Nu-Jazz", "Soul-RnB", "Celtic", "Country"),
    "bright": ("Pop", "Synth Pop", "Power-Pop", "Funk", "Kid-Friendly", "Latin", "Surf", "Chiptune", "Hip-Hop Beats"),
    "upbeat": ("Dance", "House", "Techno", "Disco", "Drum & Bass", "Breakbeat", "Bigbeat", "Garage", "Electronic"),
    "tense": ("Soundtrack", "Post-Rock", "Trip-Hop", "Industrial", "Drone", "Dubstep", "IDM"),
}


def _fs():
    from huggingface_hub import HfFileSystem

    return HfFileSystem()


def _path(i: int) -> str:
    return f"{DATASET}/train-{i:05d}-of-{N_FILES:05d}.parquet"


# ---------------------------------------------------------------- scan

def _scan_file(fs, i: int) -> tuple[list[dict], dict]:
    import pyarrow.parquet as pq

    with fs.open(_path(i), "rb", block_size=1 << 18) as fh:
        pf = pq.ParquetFile(fh)
        names = json.loads(pf.schema_arrow.metadata[b"huggingface"])["info"]["features"]
        tb = pf.read(columns=META_COLS)
        groups = [pf.metadata.row_group(g).num_rows for g in range(pf.metadata.num_row_groups)]
    licenses = names["license"]["names"]
    genres = names["genres"]["feature"]["names"]
    out, stats = [], {"rows": tb.num_rows}
    starts = [sum(groups[:g]) for g in range(len(groups))]
    for r, row in enumerate(tb.to_pylist()):
        lic = licenses[row["license"]] if row["license"] is not None else None
        if not (row["instrumental"] == 1 and lic in ALLOWED and row["allow_commercial_use"] == 1
                and row["allow_derivatives"] == 1 and row["require_share_alike"] != 1 and row["explicit"] != 1):
            continue
        g = max(k for k, s in enumerate(starts) if s <= r)
        g_names = [genres[x] for x in row["genres"] or []]
        out.append({
            "file": i, "row": r, "group": g, "pos": r - starts[g],
            "title": row["title"], "artist": row["artist"], "album": row["album_title"],
            "url": row["url"], "artist_url": row["artist_url"], "license": lic,
            "attribution": lic.startswith("CC-BY"), "listens": row["listens"] or 0,
            "genres": g_names, "tags": row["tags"] or [],
            "moods": [m for m, gs in MOOD_GENRES.items() if set(gs) & set(g_names)],
        })
    return out, stats


def cmd_scan(a) -> int:
    fs = _fs()
    cache = Path(a.out)
    done: dict[str, list] = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    todo = [i for i in range(N_FILES) if str(i) not in done]
    print(f"남은 파일 {len(todo)}/{N_FILES}", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(_scan_file, fs, i): i for i in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            i = futs[fut]
            try:
                done[str(i)] = fut.result()[0]
            except Exception as e:  # 한 파일 실패는 다음 실행 때 다시 읽는다
                print(f"  파일 {i} 실패: {e}", file=sys.stderr, flush=True)
            if n % 50 == 0 or n == len(todo):
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                found = sum(len(v) for v in done.values())
                print(f"  {n}/{len(todo)} ({time.time() - t0:.0f}초) 후보 {found}곡", flush=True)
    return 0 if len(done) == N_FILES else 1


# ---------------------------------------------------------------- fetch (parquet 페이지에서 곡 하나 꺼내기)

class _Stream:
    def __init__(self, fh):
        self.fh = fh

    def read(self, n: int) -> bytes:
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError("파일이 예상보다 짧습니다")
        return b

    def byte(self) -> int:
        return self.read(1)[0]

    def varint(self) -> int:
        shift = result = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if b < 0x80:
                return result
            shift += 7


def _thrift_value(s: _Stream, t: int):
    """thrift compact protocol 값 하나 (parquet 페이지 헤더용, 필요한 형만)."""
    if t in (1, 2):
        return t == 1
    if t == 3:
        return s.byte()
    if t in (4, 5, 6):
        v = s.varint()
        return (v >> 1) ^ -(v & 1)
    if t == 7:
        return s.read(8)
    if t == 8:
        return s.read(s.varint())
    if t in (9, 10):
        h = s.byte()
        size, et = h >> 4, h & 0x0F
        if size == 15:
            size = s.varint()
        return [_thrift_value(s, et) for _ in range(size)]
    if t == 12:
        return _thrift_struct(s)
    raise ValueError(f"모르는 thrift 형 {t}")


def _thrift_struct(s: _Stream) -> dict:
    out, fid = {}, 0
    while True:
        h = s.byte()
        if h == 0:
            return out
        delta, t = h >> 4, h & 0x0F
        if delta:
            fid += delta
        else:
            v = s.varint()
            fid = (v >> 1) ^ -(v & 1)
        out[fid] = _thrift_value(s, t)


def _snappy_chunks(s: _Stream, compressed: int):
    """snappy(raw) 블록을 앞에서부터 풀면서 풀린 조각을 차례로 내놓는다 (전부 받지 않고 멈출 수 있게).

    복사 명령은 최대 64KB 앞까지만 가리키므로 최근 1MB만 들고 있으면 된다.
    """
    start = s.fh.tell()
    s.varint()  # 풀린 전체 길이 (안 씀)
    window = bytearray()
    while s.fh.tell() - start < compressed:
        tag = s.byte()
        kind = tag & 3
        if kind == 0:  # 리터럴
            n = tag >> 2
            if n >= 60:
                n = int.from_bytes(s.read(n - 59), "little")
            piece = s.read(n + 1)
        else:
            if kind == 1:
                length, off = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | s.byte()
            elif kind == 2:
                length, off = (tag >> 2) + 1, struct.unpack("<H", s.read(2))[0]
            else:
                length, off = (tag >> 2) + 1, struct.unpack("<I", s.read(4))[0]
            buf = bytearray(window[-off:])
            for i in range(length):  # 겹치는 복사가 있어 한 바이트씩
                buf.append(buf[i])
            piece = bytes(buf[off:])
        window += piece
        if len(window) > 2 << 20:
            del window[:-(1 << 20)]
        yield piece


def _plain_values(chunks, skip_levels: bool):
    """풀린 조각들에서 PLAIN BYTE_ARRAY 값(4바이트 길이 + 내용)을 하나씩. 데이터 페이지는 앞에 정의 레벨이 붙어 있다."""
    buf, p, levels = bytearray(), 0, skip_levels
    for piece in chunks:
        buf += piece
        if levels:
            if len(buf) < 4:
                continue
            p, levels = 4 + struct.unpack("<I", buf[:4])[0], False
        while len(buf) - p >= 4:
            n = struct.unpack("<I", buf[p:p + 4])[0]
            if len(buf) - p - 4 < n:
                break
            yield bytes(buf[p + 4:p + 4 + n])
            p += 4 + n
        if p > 8 << 20:  # 다 쓴 앞부분은 버린다
            del buf[:p]
            p = 0


def extract(fs, file: int, group: int, pos: int) -> bytes:
    """file번 parquet의 group번 행 그룹에서 pos번째 곡의 오디오 바이트."""
    import pyarrow.parquet as pq

    with fs.open(_path(file), "rb", block_size=4 << 20) as fh:
        md = pq.ParquetFile(fh).metadata.row_group(group)
        col = next(md.column(j) for j in range(md.num_columns) if md.column(j).path_in_schema == "audio.bytes")
        offset = col.dictionary_page_offset if col.has_dictionary_page else col.data_page_offset
        end = offset + col.total_compressed_size
        fh.seek(offset)
        s, seen = _Stream(fh), 0
        while fh.tell() < end:
            h = _thrift_struct(s)
            ptype, raw, comp = h[1], h[2], h[3]
            body = fh.tell()
            plain = ptype == 2 or (ptype == 0 and h.get(5, {}).get(2) == 0)  # 사전 페이지, 또는 PLAIN 데이터 페이지
            if plain:
                if col.compression == "SNAPPY":
                    chunks = _snappy_chunks(s, comp)
                elif col.compression == "UNCOMPRESSED":
                    chunks = iter(lambda: s.read(min(1 << 20, body + comp - fh.tell())) if fh.tell() < body + comp else b"", b"")
                else:
                    raise ValueError(f"모르는 압축 {col.compression}")
                for v in _plain_values(chunks, skip_levels=ptype == 0):
                    if seen == pos:  # 곡이 다 들어오면 나머지는 받지 않고 멈춘다
                        return v
                    seen += 1
            fh.seek(body + comp)
    raise LookupError(f"파일 {file} 그룹 {group}에서 {pos}번째 곡을 찾지 못함")


def cmd_fetch(a) -> int:
    cat = json.loads(Path(a.catalog).read_text(encoding="utf-8"))
    rows = [r for v in cat.values() for r in v]
    picks = [r for r in rows if f"{r['file']}:{r['row']}" in set(a.ids)]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fs = _fs()
    for r in picks:
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"{r['artist']}-{r['title']}").strip("-")[:60]
        dst = out / f"{r['file']:03d}-{r['row']:03d}-{name}.mp3"
        if dst.exists():
            print(f"있음: {dst.name}")
            continue
        t0 = time.time()
        data = extract(fs, r["file"], r["group"], r["pos"])
        dst.write_bytes(data)
        (dst.with_suffix(".json")).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"받음: {dst.name} ({len(data) / 1e6:.1f}MB, {time.time() - t0:.0f}초)", flush=True)
    return 0


# ---------------------------------------------------------------- add (라이브러리에 넣기)

ROOT = Path(__file__).resolve().parent.parent
EXCERPT_S = 70  # 릴스·쇼츠는 30초 안팎이라 넉넉히. 더 긴 영상은 렌더할 때 처음부터 이어 붙인다
TARGET = "I=-16:TP=-1.5:LRA=11"  # 합성 음악(bgm.py)과 같은 체감 음량


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _auto_start(src: Path) -> float:
    """앞부분이 조용하면(인트로) 소리가 충분히 커지는 지점부터. 릴스는 첫 1~2초에 넘겨지므로."""
    import subprocess

    import numpy as np

    sr = 11025
    raw = subprocess.run([_ffmpeg(), "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(sr),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    win = sr // 2
    rms = np.sqrt((x[: len(x) // win * win].reshape(-1, win) ** 2).mean(axis=1))
    level = np.median(rms)
    if rms[:20].mean() >= 0.6 * level:  # 첫 10초가 충분히 크면 처음부터
        return 0.0
    smooth = np.convolve(rms, np.ones(6) / 6, "same")  # 3초 평균
    latest = max(0, len(rms) - EXCERPT_S * 2)
    hit = next((i for i in range(latest + 1) if smooth[i] >= 0.8 * level), 0)
    return hit / 2


def cmd_add(a) -> int:
    import subprocess

    src = Path(a.mp3)
    info = json.loads(src.with_suffix(".json").read_text(encoding="utf-8"))
    start = a.start if a.start is not None else _auto_start(src)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{info['artist']}-{info['title']}".lower()).strip("-")[:50]
    rel = f"{a.mood}/{slug}.m4a"
    dst = ROOT / "music" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    cut = ["-ss", f"{start:.2f}", "-t", str(EXCERPT_S), "-i", str(src)]
    r = subprocess.run([_ffmpeg(), "-hide_banner", "-nostats", *cut, "-af", f"loudnorm={TARGET}:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = json.loads(r.stderr[r.stderr.rindex("{"):])
    norm = (f"loudnorm={TARGET}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", *cut, "-af", norm, "-ar", "44100", "-ac", "2",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)], check=True)

    lib_path = ROOT / "music" / "library.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else {"tracks": []}
    entry = {"file": rel, "mood": a.mood, "title": info["title"], "artist": info["artist"],
             "license": info["license"], "attribution": info["attribution"], "url": info["url"],
             "artist_url": info.get("artist_url"), "source": f"FMA (Hugging Face benjamin-paine/free-music-archive-full) {info['file']}:{info['row']}",
             "start": round(start, 1), "seconds": EXCERPT_S}
    lib["tracks"] = [t for t in lib["tracks"] if t["file"] != rel] + [entry]
    lib["tracks"].sort(key=lambda t: t["file"])
    lib_path.write_text(json.dumps(lib, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"추가: music/{rel} ({start:.1f}초부터 {EXCERPT_S}초, 원래 {float(m['input_i']):.1f} LUFS → -16)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fma", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--out", default=".cache/fma_catalog.json")
    p.add_argument("--workers", type=int, default=12)
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("fetch")
    p.add_argument("ids", nargs="+", help="file:row (scan 결과의 file·row)")
    p.add_argument("--catalog", default=".cache/fma_catalog.json")
    p.add_argument("--out", default=".cache/fma_tracks")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("add", help="받은 mp3를 music/<분위기>/에 70초 발췌·-16 LUFS m4a로 넣고 library.json에 등록")
    p.add_argument("mp3")
    p.add_argument("--mood", required=True, choices=list(MOOD_GENRES))
    p.add_argument("--start", type=float, default=None, help="발췌 시작(초). 비우면 인트로를 건너뛰어 자동")
    p.set_defaults(fn=cmd_add)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
