import asyncio, csv, gzip, html, io, json, logging, os, random, re, secrets
import shutil, sqlite3, subprocess, tempfile, time, zipfile
from pathlib import Path

import httpx
from PIL import Image

# ───────────────────────── تنظیمات ─────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
API_BASE = os.getenv("API_BASE", "https://api.vibegram.chat").rstrip("/")
LINK_EMOJI = os.getenv("LINK_EMOJI", "https://t.me/addemoji/{name}")
LINK_STICKER = os.getenv("LINK_STICKER", "https://t.me/addstickers/{name}")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
ADMIN_ID = 1780243320
EMOJI_MAX = 120
STICKER_MAX = 120
DEFAULT_EMOJI = "🌱"  # فقط برای فیلد اجباری emoji_list در API، در متن ربات نمایش داده نمی‌شود
ALLOWED = {".tgs", ".json", ".webp", ".webm", ".mp4", ".png"}
MIME = {".tgs": "application/x-tgsticker", ".webp": "image/webp", ".webm": "video/webm"}
FMT_LABEL = {"animated": "TGS", "video": "WebM", "static": "WebP"}
START_TIME = time.time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
  username TEXT, joined INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS packs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
  title TEXT, name TEXT, link TEXT, count INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
""")


def q(sql, args=()):
    return db.execute(sql, args).fetchall()


def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return r[0][0] if r else default


def kv_set(k, v):
    q("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ───────────────────────── کمک‌ها ─────────────────────────
def pd(x):
    return str(x).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def esc(s):
    return html.escape(str(s or ""))


def bar(x, y):
    filled = 0 if y == 0 else min(5, x * 5 // y)
    return "▰" * filled + "▱" * (5 - filled)


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def btn(text, data, style=None):
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style  # primary=آبی | success=سبز | danger=قرمز
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


class ApiError(Exception):
    def __init__(self, desc, retry_after=0):
        super().__init__(desc)
        self.retry_after = retry_after


class Api:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20))
        self.url = f"{API_BASE}/bot{TOKEN}/"

    async def call(self, method, files=None, **params):
        data = {}
        for k, v in params.items():
            if v is None:
                continue
            data[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        r = await self.http.post(self.url + method, data=data, files=files)
        try:
            j = r.json()
        except Exception:
            raise ApiError(f"HTTP {r.status_code}")
        if not j.get("ok"):
            raise ApiError(j.get("description", "error"), (j.get("parameters") or {}).get("retry_after", 0))
        return j


api = Api()
BOT_USERNAME = ""


async def send(chat, text, markup=None):
    j = await api.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                       reply_markup=markup, link_preview_options={"is_disabled": True})
    return j["result"]


async def edit(chat, mid, text, markup=None):
    try:
        await api.call("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                       reply_markup=markup, link_preview_options={"is_disabled": True})
    except ApiError as e:
        if "not modified" not in str(e):
            raise


async def toast(cb_id, text=None):
    try:
        await api.call("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=False)
    except ApiError:
        pass


async def download(file_id, dest):
    info = await api.call("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    async with api.http.stream("GET", f"{API_BASE}/file/bot{TOKEN}/{path}") as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)


# ───────────────────────── متن‌ها ─────────────────────────
def start_text(name):
    return f"سلام <b>{esc(name)}</b> ، به <b><u>پکیج‌ساز اختصاصی وایب‌موجی</u></b> خوش آمدید. 🌱"


def start_kb():
    return kb(
        [btn("ساخت ایموجی", "mk:emoji", "success"), btn("ساخت استیکر", "mk:sticker", "success")],
        [btn("مدیریت پک‌های ساخته شده", "manage", "primary")],
        [btn("خرید اشتراک", "buy", "danger"), btn("مشاهده راهنما", "help", "danger")],
    )


KIND_FA = {"emoji": ("ایموجی", "ایموجی‌ها", "ایموجی‌های", EMOJI_MAX),
           "sticker": ("استیکر", "استیکرها", "استیکرهای", STICKER_MAX)}


def ask_file_text(kind):
    s, _, sp, mx = KIND_FA[kind]
    return (f"لطفا ، فایل زیپ « <b>ZIP</b> » شامل {s}‌های پرمیوم با فرمت « "
            f"<u>TGS  Lottie JSON  WebP  WebM  MP4</u> » را ارسال کنید.\n\n"
            f"✦ توجه داشته باشید که هر پک {s} حداکثر <b>{pd(mx)}</b> {s} ظرفیت دارد !")


HELP_TEXT = """<b><u>راهنمای ساخت پک</u></b>

<b>۱. آماده‌سازی فایل‌ها</b>
• فایل‌ها را داخل یک فایل <u>ZIP</u> قرار دهید، یا فایل‌ها را تکی ارسال کنید.
• فرمت‌های مجاز: <u>TGS</u>، <u>Lottie JSON</u>، <u>WebP</u>، <u>PNG</u>، <u>WebM</u>، <u>MP4</u>
• حداکثر تعداد در هر پک: <b>۱۲۰</b> فایل

<b>۲. ساخت پک</b>
• یکی از گزینه‌های «ساخت ایموجی» یا «ساخت استیکر» را انتخاب کنید.
• فایل زیپ (یا فایل‌های تکی) را ارسال کنید و تا پایان پردازش صبر کنید.
• عنوان دلخواه را وارد کنید یا با عنوان پیشفرض ادامه دهید.
• لینک پک ساخته‌شده برای شما ارسال می‌شود.

<b>۳. استانداردهای فایل</b>
• TGS و Lottie JSON: ابعاد <b>۵۱۲×۵۱۲</b>، حداکثر <b>۳</b> ثانیه، حجم نهایی حداکثر <b>۶۴</b> کیلوبایت
• WebM و MP4: به‌صورت خودکار به استاندارد ویدیویی (حداکثر ۳ ثانیه) تبدیل می‌شوند
• WebP و PNG: به‌صورت خودکار اندازه‌دهی می‌شوند
• اگر فایل‌های شما چند فرمت متفاوت داشته باشند، برای هر فرمت یک پک جداگانه ساخته می‌شود.

<b>۴. مدیریت</b>
• همه پک‌های ساخته‌شده شما در بخش «مدیریت پک‌های ساخته شده» قابل مشاهده است.


قدرت گرفته از : @Green
کانال وایب‌موجی : @PremiumEmoji"""

# ───────────────────────── وضعیت‌ها ─────────────────────────
sessions = {}      # uid -> dict
admin_state = {}   # "mode": broadcast|ban|unban|search, "pending": (chat, mid)
CONV_SEM = asyncio.Semaphore(2)


def reset_session(uid):
    s = sessions.pop(uid, None)
    if s:
        if s.get("timer"):
            s["timer"].cancel()
        shutil.rmtree(s.get("dir", ""), ignore_errors=True)


def extract_file(m):
    if "document" in m:
        d = m["document"]
        return {"file_id": d["file_id"], "name": d.get("file_name", "file"), "size": d.get("file_size", 0)}
    if "sticker" in m:
        s = m["sticker"]
        ext = ".tgs" if s.get("is_animated") else ".webm" if s.get("is_video") else ".webp"
        return {"file_id": s["file_id"], "name": s["file_unique_id"] + ext, "size": s.get("file_size", 0)}
    for key in ("video", "animation"):
        if key in m:
            v = m[key]
            return {"file_id": v["file_id"], "name": v.get("file_name") or v["file_unique_id"] + ".mp4",
                    "size": v.get("file_size", 0)}
    return None


# ───────────────────────── تبدیل فایل‌ها ─────────────────────────
def lottie_to_tgs(data, dest):
    if not all(k in data for k in ("layers", "w", "h", "fr", "ip", "op")):
        raise ValueError("not a lottie file")
    if int(data["w"]) != 512 or int(data["h"]) != 512:
        raise ValueError("lottie must be 512x512")
    if data["fr"] > 60 or (data["op"] - data["ip"]) / data["fr"] > 3.05:
        raise ValueError("lottie too long / fps too high")
    raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 9, mtime=0)
    if len(raw) > 64 * 1024:
        raise ValueError("tgs larger than 64KB")
    dest.write_bytes(raw)
    return dest, "animated"


def convert(src: Path, out_dir: Path, idx: int, kind: str):
    px = 100 if kind == "emoji" else 512
    ext = src.suffix.lower()
    out = out_dir / f"{idx:04d}"

    if ext == ".tgs":
        return lottie_to_tgs(json.loads(gzip.decompress(src.read_bytes())), out.with_suffix(".tgs"))
    if ext == ".json":
        return lottie_to_tgs(json.loads(src.read_text("utf-8")), out.with_suffix(".tgs"))

    if ext in (".webp", ".png"):
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                raise ValueError("animated image not supported")
            im = im.convert("RGBA")
        sc = px / max(im.size)
        im = im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))), Image.LANCZOS)
        if kind == "emoji":
            canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            canvas.paste(im, ((px - im.width) // 2, (px - im.height) // 2))
            im = canvas
        dest = out.with_suffix(".webp")
        for qlt in (95, 85, 75, 60, 45):
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=qlt)
            if buf.tell() <= 64 * 1024:
                dest.write_bytes(buf.getvalue())
                return dest, "static"
        raise ValueError("webp larger than 64KB")

    if ext in (".webm", ".mp4"):
        vf = (f"fps=30,scale={px}:{px}:force_original_aspect_ratio=decrease,"
              f"scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuva420p")
        if kind == "emoji":
            vf += f",pad={px}:{px}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
        dest = out.with_suffix(".webm")
        for crf in (30, 38, 46, 54):
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", "3", "-an", "-vf", vf,
                 "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", str(crf),
                 "-auto-alt-ref", "0", "-row-mt", "1", str(dest)],
                capture_output=True, timeout=180)
            if r.returncode != 0:
                raise ValueError("ffmpeg: " + r.stderr.decode(errors="ignore")[-200:])
            if dest.stat().st_size <= 256 * 1024:
                return dest, "video"
        raise ValueError("video larger than 256KB")
    raise ValueError("unsupported format")


def expand(src: Path, out: Path):
    ext = src.suffix.lower()
    res = []
    if ext == ".zip":
        # Each archive gets its own directory, avoiding collisions between ZIPs.
        zip_dir = out / f"zip_{secrets.token_hex(4)}"
        zip_dir.mkdir(parents=True, exist_ok=True)
        max_entries, max_uncompressed = 120, 100 * 1024 * 1024
        extracted_bytes = 0
        with zipfile.ZipFile(src) as z:
            infos = sorted(z.infolist(), key=lambda i: natkey(i.filename))
            for info in infos:
                p = Path(info.filename)
                if info.is_dir() or "__MACOSX" in info.filename or p.name.startswith("."):
                    continue
                e = p.suffix.lower()
                if e not in ALLOWED or info.file_size > 30 * 1024 * 1024:
                    continue
                if info.flag_bits & 0x1:
                    continue  # encrypted entries are unsupported
                if len(res) >= max_entries:
                    raise ValueError("ZIP contains too many supported files (max 120)")
                extracted_bytes += info.file_size
                if extracted_bytes > max_uncompressed:
                    raise ValueError("ZIP expands beyond the 100 MB safety limit")
                # Flatten paths and give each entry a unique index to prevent overwrite/path traversal.
                safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", p.stem)[:40] or "file"
                dest = zip_dir / f"{len(res):04d}_{safe_stem}{e}"
                with z.open(info) as source, open(dest, "wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                res.append(dest)
    elif ext in ALLOWED:
        res.append(src)
    return res


# ───────────────────────── جریان ساخت پک ─────────────────────────
async def begin(uid, chat, kind):
    reset_session(uid)
    sessions[uid] = {"kind": kind, "stage": "await_file", "pending": [], "dir": tempfile.mkdtemp(prefix="vm_")}
    await send(chat, ask_file_text(kind))


async def add_file(uid, chat, f):
    s = sessions[uid]
    s["stage"] = "collecting"
    s["pending"].append(f)
    if s.get("timer"):
        s["timer"].cancel()

    async def go():
        await asyncio.sleep(2.5)
        await process(uid, chat)

    s["timer"] = asyncio.create_task(go())


async def process(uid, chat):
    s = sessions.get(uid)
    if not s or s["stage"] != "collecting":
        return
    s["stage"] = "busy"
    kind = s["kind"]
    fa, fa_pl, _, mx = KIND_FA[kind]
    work = Path(s["dir"])
    (work / "in").mkdir(exist_ok=True)
    (work / "out").mkdir(exist_ok=True)
    msg = await send(chat, f"فایل دریافت شد ، درحال پردازش {fa_pl}\n{bar(0, 0)} | [ {pd(0)} - {pd(0)} ]")
    mid = msg["message_id"]
    try:
        cands = []
        zip_stem = None
        for i, f in enumerate(s["pending"]):
            dest = work / "in" / f"{i:03d}_{re.sub(r'[^A-Za-z0-9._-]', '_', f['name'])}"
            await download(f["file_id"], dest)
            if dest.suffix.lower() == ".zip" and not zip_stem:
                zip_stem = Path(f["name"]).stem
            cands += await asyncio.to_thread(expand, dest, work / "in")
        if len(s["pending"]) > 1 or not zip_stem:
            zip_stem = zip_stem or Path(s["pending"][0]["name"]).stem
        total = len(cands)
        if total == 0:
            await edit(chat, mid, "فرمت فایل پشتیبانی نمی‌شود یا فایل خالی است !")
            return await finish_to_start(uid, chat)
        if total > mx:
            await edit(chat, mid, f"تعداد فایل‌ها بیش از حد مجاز است ! (حداکثر <b>{pd(mx)}</b>)")
            return await finish_to_start(uid, chat)

        items, failed, done, last_fill = [], 0, 0, -1
        for i, c in enumerate(cands):
            try:
                async with CONV_SEM:
                    items.append(await asyncio.to_thread(convert, c, work / "out", i, kind))
            except Exception as e:
                failed += 1
                log.warning("convert failed %s: %s", c.name, e)
            done += 1
            fill = done * 5 // total
            if fill != last_fill:
                last_fill = fill
                await edit(chat, mid, f"فایل دریافت شد ، درحال پردازش {fa_pl}\n"
                                      f"{bar(done, total)} | [ {pd(done)} - {pd(total)} ]")
        if not items:
            await edit(chat, mid, f"هیچ‌کدام از فایل‌ها قابل پردازش نبود !")
            return await finish_to_start(uid, chat)

        stem = re.sub(r"\s+", "_", zip_stem or "pack")[:50]
        s["title"] = f"{stem}_{random.randint(1000, 9999)}"
        s["items"] = items
        s["stage"] = "await_choice"
        fail_line = f"\nفایل‌های ناموفق : <b>{pd(failed)}</b>" if failed else ""
        await edit(
            chat, mid,
            f"<b>پردازش {fa_pl} موفقیت‌آمیز بود !</b>\n\n"
            f"{bar(done, total)} | [ {pd(len(items))} - {pd(total)} ]\n\n"
            f"{fa_pl} دریافت شده : <b>{pd(len(items))}</b>\n"
            f"تمامی {fa_pl} : <b>{pd(total)}</b>{fail_line}\n\n"
            f"عنوان فعلی پک شما : [ <u>{esc(s['title'])}</u> ]",
            kb([btn("انتخاب عنوان سفارشی", "title:custom", "success"),
                btn("ادامه با پیشفرض", "title:default", "success")],
               [btn(f"مشاهده {fa_pl}", "view", "primary")]))
    except Exception as e:
        log.exception("process failed")
        await send(chat, f"پردازش با خطا مواجه شد : <code>{esc(e)}</code>")
        await finish_to_start(uid, chat)


async def finish_to_start(uid, chat):
    reset_session(uid)
    await asyncio.sleep(3)
    await send(chat, start_text(user_name(uid)), start_kb())


def user_name(uid):
    r = q("SELECT first_name,last_name FROM users WHERE id=?", (uid,))
    return " ".join(x for x in (r[0] if r else ()) if x) or "کاربر"


async def create_packs(uid, chat):
    s = sessions.get(uid)
    if not s or s["stage"] not in ("await_choice", "await_title"):
        return
    s["stage"] = "busy"
    kind, title = s["kind"], s["title"]
    fa, fa_pl, fa_sp, _ = KIND_FA[kind]
    groups = {}
    for path, fmt in s["items"]:
        groups.setdefault(fmt, []).append(path)
    blocks = []
    try:
        for fmt, files in groups.items():
            name = f"vm{secrets.token_hex(5)}_by_{BOT_USERNAME}"
            t = title if len(groups) == 1 else f"{title} · {FMT_LABEL[fmt]}"
            t = t[:64]

            def inp(i, p):
                return {"sticker": f"attach://f{i}", "format": fmt, "emoji_list": [DEFAULT_EMOJI]}

            first = files[:50]
            await api.call(
                "createNewStickerSet",
                files={f"f{i}": (p.name, p.read_bytes(), MIME[p.suffix]) for i, p in enumerate(first)},
                user_id=uid, name=name, title=t,
                sticker_type="custom_emoji" if kind == "emoji" else "regular",
                stickers=[inp(i, p) for i, p in enumerate(first)])
            for p in files[50:]:
                await api.call("addStickerToSet", files={"f0": (p.name, p.read_bytes(), MIME[p.suffix])},
                               user_id=uid, name=name, sticker=inp(0, p))
            link = (LINK_EMOJI if kind == "emoji" else LINK_STICKER).format(name=name)
            q("INSERT INTO packs(user_id,kind,title,name,link,count,created) VALUES(?,?,?,?,?,?,?)",
              (uid, kind, t, name, link, len(files), int(time.time())))
            blocks.append(f"عنوان پک : <b>{esc(t)}</b>\nتعداد : <b>{pd(len(files))}</b>\nلینک : {link}")
        await send(chat, f"<b>پک {fa_sp} شما با موفقیت ساخته شد !</b>\n\n" + "\n\n".join(blocks))
    except Exception as e:
        log.exception("create failed")
        await send(chat, f"ساخت پک با خطا مواجه شد : <code>{esc(e)}</code>")
    await finish_to_start(uid, chat)


async def show_packs(chat, uid):
    out = []
    for kind, head in (("emoji", "ایموجی‌های ساخته شده:"), ("sticker", "استیکرهای ساخته شده:")):
        rows = q("SELECT title,link FROM packs WHERE user_id=? AND kind=? ORDER BY id", (uid, kind))
        lines = [f"<b><u>{head}</u></b>\n"]
        lines += [f"{pd(i)}. {esc(t)} - {l}" for i, (t, l) in enumerate(rows, 1)] or ["—"]
        out.append("\n".join(lines))
    text, chunks = "\n\n".join(out), []
    while len(text) > 3800:
        cut = text.rfind("\n", 0, 3800)
        chunks.append(text[:cut])
        text = text[cut + 1:]
    for c in chunks + [text]:
        await send(chat, c)


# ───────────────────────── پنل ادمین ─────────────────────────
def admin_kb():
    on = kv_get("enabled", "1") == "1"
    return kb(
        [btn("آمار ربات", "adm:stats", "primary"), btn("کاربران", "adm:users:0", "primary")],
        [btn("پک‌های ساخته شده", "adm:packs:0", "primary"), btn("ارسال همگانی", "adm:bc", "success")],
        [btn("خاموش کردن ربات" if on else "روشن کردن ربات", "adm:toggle", "danger" if on else "success")],
        [btn("مسدود کردن کاربر", "adm:ban", "danger"), btn("رفع مسدودی", "adm:unban", "success")],
        [btn("جستجوی کاربر", "adm:search", "primary"), btn("خروجی CSV کاربران", "adm:csv", "primary")],
    )


def admin_text():
    return f"<b><u>پنل مدیریت</u></b>\n\nوضعیت ربات : <b>{'روشن' if kv_get('enabled', '1') == '1' else 'خاموش'}</b>"


def stats_text():
    def n(sql, args=()):
        return q(sql, args)[0][0] or 0

    day = int(time.time()) - 86400
    up = int(time.time() - START_TIME)
    total_users = n("SELECT COUNT(*) FROM users")
    new_users = n("SELECT COUNT(*) FROM users WHERE joined>?", (day,))
    banned = n("SELECT COUNT(*) FROM users WHERE banned=1")
    emoji_packs = n("SELECT COUNT(*) FROM packs WHERE kind='emoji'")
    sticker_packs = n("SELECT COUNT(*) FROM packs WHERE kind='sticker'")
    items = n("SELECT SUM(count) FROM packs")
    return (
        "<b><u>آمار ربات</u></b>\n\n"
        f"کل کاربران : <b>{pd(total_users)}</b>\n"
        f"کاربران جدید (۲۴ ساعت) : <b>{pd(new_users)}</b>\n"
        f"کاربران مسدود : <b>{pd(banned)}</b>\n"
        f"پک‌های ایموجی : <b>{pd(emoji_packs)}</b>\n"
        f"پک‌های استیکر : <b>{pd(sticker_packs)}</b>\n"
        f"مجموع آیتم‌های ساخته‌شده : <b>{pd(items)}</b>\n"
        f"زمان فعالیت : <b>{pd(up // 3600)}</b> ساعت و <b>{pd(up % 3600 // 60)}</b> دقیقه")


def page_kb(prefix, page, total, per):
    row = []
    if page > 0:
        row.append(btn("قبلی", f"adm:{prefix}:{page - 1}", "primary"))
    if (page + 1) * per < total:
        row.append(btn("بعدی", f"adm:{prefix}:{page + 1}", "primary"))
    return kb(row, [btn("بازگشت به پنل", "adm:home", "danger")]) if row else kb([btn("بازگشت به پنل", "adm:home", "danger")])


async def broadcast(chat, src_chat, src_mid):
    ids = [r[0] for r in q("SELECT id FROM users WHERE banned=0")]
    ok = fail = 0
    st = await send(chat, f"در حال ارسال همگانی ...\n{bar(0, len(ids))} | [ {pd(0)} - {pd(len(ids))} ]")
    for i, uid in enumerate(ids, 1):
        for attempt in range(2):
            try:
                await api.call("copyMessage", chat_id=uid, from_chat_id=src_chat, message_id=src_mid)
                ok += 1
                break
            except ApiError as e:
                if e.retry_after and attempt == 0:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                fail += 1
                break
        await asyncio.sleep(0.05)
        if i % 40 == 0:
            await edit(chat, st["message_id"], f"در حال ارسال همگانی ...\n{bar(i, len(ids))} | [ {pd(i)} - {pd(len(ids))} ]")
    await edit(chat, st["message_id"],
               f"<b>ارسال همگانی پایان یافت !</b>\n\nموفق : <b>{pd(ok)}</b>\nناموفق : <b>{pd(fail)}</b>")


def user_card(r):
    uid, fn, ln, un, joined, banned = r
    packs = q("SELECT COUNT(*) FROM packs WHERE user_id=?", (uid,))[0][0]
    return (f"آیدی : <code>{uid}</code>\nنام : {esc(' '.join(x for x in (fn, ln) if x))}\n"
            f"یوزرنیم : {'@' + esc(un) if un else '—'}\nتعداد پک : <b>{pd(packs)}</b>\n"
            f"وضعیت : <b>{'مسدود' if banned else 'فعال'}</b>")


async def admin_callback(cb):
    data, chat, mid = cb["data"], cb["message"]["chat"]["id"], cb["message"]["message_id"]
    parts = data.split(":")
    act = parts[1]
    await toast(cb["id"])
    if act == "home":
        admin_state.clear()
        await edit(chat, mid, admin_text(), admin_kb())
    elif act == "stats":
        await edit(chat, mid, stats_text(), kb([btn("بازگشت به پنل", "adm:home", "danger")]))
    elif act == "toggle":
        kv_set("enabled", "0" if kv_get("enabled", "1") == "1" else "1")
        await edit(chat, mid, admin_text(), admin_kb())
    elif act == "users":
        page, per = int(parts[2]), 15
        total = q("SELECT COUNT(*) FROM users")[0][0]
        rows = q("SELECT id,first_name,last_name,username FROM users ORDER BY joined LIMIT ? OFFSET ?", (per, page * per))
        lines = [f"{pd(page * per + i)}. <code>{u}</code> | {esc(' '.join(x for x in (f, l) if x))} | "
                 f"{'@' + esc(n) if n else '—'}" for i, (u, f, l, n) in enumerate(rows, 1)]
        await edit(chat, mid, f"<b><u>کاربران</u></b> ({pd(total)})\n\n" + ("\n".join(lines) or "—"),
                   page_kb("users", page, total, per))
    elif act == "packs":
        page, per = int(parts[2]), 10
        total = q("SELECT COUNT(*) FROM packs")[0][0]
        rows = q("SELECT kind,title,link,user_id FROM packs ORDER BY id DESC LIMIT ? OFFSET ?", (per, page * per))
        lines = [f"{pd(page * per + i)}. {'ایموجی' if k == 'emoji' else 'استیکر'} | {esc(t)}\n{l}\nسازنده : <code>{u}</code>"
                 for i, (k, t, l, u) in enumerate(rows, 1)]
        await edit(chat, mid, f"<b><u>پک‌های ساخته شده</u></b> ({pd(total)})\n\n" + ("\n\n".join(lines) or "—"),
                   page_kb("packs", page, total, per))
    elif act in ("bc", "ban", "unban", "search"):
        admin_state.clear()
        admin_state["mode"] = {"bc": "broadcast"}.get(act, act)
        prompt = {"bc": "پیام مورد نظر برای ارسال همگانی را بفرستید (متن، عکس، ویدیو، استیکر، ایموجی پرمیوم و ... همراه با فرمت‌ها) :",
                  "ban": "آیدی عددی کاربر را برای مسدود کردن ارسال کنید :",
                  "unban": "آیدی عددی کاربر را برای رفع مسدودی ارسال کنید :",
                  "search": "آیدی عددی یا یوزرنیم کاربر را ارسال کنید :"}[act]
        await edit(chat, mid, prompt, kb([btn("لغو", "adm:home", "danger")]))
    elif act == "bc_go":
        p = admin_state.pop("pending", None)
        admin_state.clear()
        if p:
            await edit(chat, mid, "ارسال همگانی آغاز شد ...")
            asyncio.create_task(broadcast(chat, *p))
    elif act == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "first_name", "last_name", "username", "joined", "banned"])
        w.writerows(q("SELECT id,first_name,last_name,username,joined,banned FROM users"))
        await api.call("sendDocument", files={"document": ("users.csv", buf.getvalue().encode("utf-8-sig"), "text/csv")},
                       chat_id=chat)


async def admin_message(m):
    chat, text = m["chat"]["id"], m.get("text", "")
    mode = admin_state.get("mode")
    if mode == "broadcast":
        admin_state["pending"] = (chat, m["message_id"])
        await api.call("copyMessage", chat_id=chat, from_chat_id=chat, message_id=m["message_id"])
        n = q("SELECT COUNT(*) FROM users WHERE banned=0")[0][0]
        await send(chat, f"پیش‌نمایش بالا برای <b>{pd(n)}</b> کاربر ارسال شود؟",
                   kb([btn("تایید و ارسال", "adm:bc_go", "success"), btn("لغو", "adm:home", "danger")]))
    elif mode in ("ban", "unban"):
        if not text.strip().lstrip("-").isdigit():
            return await send(chat, "آیدی عددی معتبر ارسال کنید :")
        q("UPDATE users SET banned=? WHERE id=?", (1 if mode == "ban" else 0, int(text)))
        admin_state.clear()
        await send(chat, "انجام شد.", kb([btn("بازگشت به پنل", "adm:home", "primary")]))
    elif mode == "search":
        t = text.strip().lstrip("@")
        r = q("SELECT * FROM users WHERE id=?", (int(t),)) if t.isdigit() else q("SELECT * FROM users WHERE lower(username)=lower(?)", (t,))
        admin_state.clear()
        await send(chat, user_card(r[0]) if r else "کاربری یافت نشد.", kb([btn("بازگشت به پنل", "adm:home", "primary")]))


# ───────────────────────── هندلرها ─────────────────────────
def upsert_user(u):
    q("INSERT OR IGNORE INTO users(id,first_name,last_name,username,joined) VALUES(?,?,?,?,?)",
      (u["id"], u.get("first_name"), u.get("last_name"), u.get("username"), int(time.time())))
    q("UPDATE users SET first_name=?,last_name=?,username=? WHERE id=?",
      (u.get("first_name"), u.get("last_name"), u.get("username"), u["id"]))


def is_banned(uid):
    r = q("SELECT banned FROM users WHERE id=?", (uid,))
    return bool(r and r[0][0])


async def on_message(m):
    if m["chat"]["type"] != "private" or "from" not in m:
        return
    u, chat, text = m["from"], m["chat"]["id"], m.get("text", "")
    uid = u["id"]
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/ad":
        if uid == ADMIN_ID:  # برای بقیه هیچ پاسخی داده نمی‌شود
            admin_state.clear()
            await send(chat, admin_text(), admin_kb())
        return
    upsert_user(u)
    if uid != ADMIN_ID:
        if is_banned(uid):
            return
        if kv_get("enabled", "1") != "1":
            return await send(chat, "ربات درحال‌حاضر خاموش است !")
    if cmd == "/start":
        reset_session(uid)
        admin_state.clear() if uid == ADMIN_ID else None
        return await send(chat, start_text(u.get("first_name", "")), start_kb())
    if uid == ADMIN_ID and admin_state.get("mode"):
        return await admin_message(m)
    s = sessions.get(uid)
    if text and s and s["stage"] == "await_title":
        s["title"] = text.strip()[:64]
        return await create_packs(uid, chat)
    f = extract_file(m)
    if f and s and s["stage"] in ("await_file", "collecting"):
        return await add_file(uid, chat, f)
    if f and not s:
        await send(chat, start_text(u.get("first_name", "")), start_kb())


async def on_callback(cb):
    u, data = cb["from"], cb.get("data", "")
    uid, chat = u["id"], cb["message"]["chat"]["id"]
    if data.startswith("adm:"):
        if uid == ADMIN_ID:
            await admin_callback(cb)
        return
    upsert_user(u)
    if uid != ADMIN_ID and (is_banned(uid) or kv_get("enabled", "1") != "1"):
        return await toast(cb["id"])
    s = sessions.get(uid)
    if data in ("mk:emoji", "mk:sticker"):
        await toast(cb["id"])
        await begin(uid, chat, data.split(":")[1])
    elif data == "manage":
        await toast(cb["id"])
        await show_packs(chat, uid)
    elif data == "buy":
        await toast(cb["id"], "درحال‌حاضر امکان خرید اشتراک وجود ندارد !")
    elif data == "help":
        await toast(cb["id"])
        await send(chat, HELP_TEXT)
    elif data == "view":
        await toast(cb["id"], "این قابلیت به‌زودی فعال خواهد شد.")
    elif data == "title:custom" and s and s["stage"] == "await_choice":
        await toast(cb["id"])
        s["stage"] = "await_title"
        await send(chat, "عنوان مورد نظر خود را ارسال کنید :")
    elif data == "title:default" and s and s["stage"] == "await_choice":
        await toast(cb["id"])
        await create_packs(uid, chat)
    else:
        await toast(cb["id"])


async def safe(coro):
    try:
        await coro
    except Exception:
        log.exception("handler error")


async def main():
    global BOT_USERNAME
    BOT_USERNAME = (await api.call("getMe"))["result"]["username"]
    log.info("started as @%s via %s", BOT_USERNAME, API_BASE)
    offset = 0
    while True:
        try:
            ups = (await api.call("getUpdates", offset=offset, timeout=50,
                                  allowed_updates=["message", "callback_query"]))["result"]
        except Exception as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            if "message" in u:
                asyncio.create_task(safe(on_message(u["message"])))
            elif "callback_query" in u:
                asyncio.create_task(safe(on_callback(u["callback_query"])))


if __name__ == "__main__":
    asyncio.run(main())
