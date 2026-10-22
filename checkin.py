#!/usr/bin/env python3
"""「晚安成电」签到脚本。

快速开始
--------
    python3 checkin.py

按提示选一种登录方式即可：

  1. 手机读取  需要 adb 已连接 + 手机已 root + 小程序里打开过打卡页
  2. 浏览器 Cookie  电脑上已在浏览器登录 jzsz.uestc.edu.cn
                     （F12 -> Application -> Cookies -> SESSION）
  3. sessionId  小程序 H5 地址栏 ?sessionId= 后面那串

登录成功后会把会话缓存在 ~/.cache/goodnight-uestc/，下次自动复用。

常用参数
--------
    --yes             跳过确认，直接提交
    --dry-run         只看状态，不提交
    --lat/--lon       指定提交坐标（默认用后端下发的打卡点中心）
    --accuracy        上报的定位精度，默认 18
    --from-device     直接尝试从手机读取 sessionId
    --session-id ID   直接指定 sessionId
    --cookie COOKIE   直接指定 Cookie，例如 "SESSION=xxxx"
    --logout          清除本地缓存的会话

说明
----
脚本只提交经纬度，不读取真实定位；服务端会按你提交的坐标计算距离并落库。
打卡时段由服务端强制，脚本会在提交前先检查。
"""

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://jzsz.uestc.edu.cn/daily-checkin/api"
APPID = "wxaa78bf2905f01cee"

UA = (
    "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.79"
)

CST = timezone(timedelta(hours=8))

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "goodnight-uestc"
JAR_PATH = CACHE_DIR / "session.txt"

REPORTED = {"STUDENT_REPORTED", "INSTRUCTOR_REPORTED"}


# ---------------------------------------------------------------- 输出

def info(msg=""):
    print(msg)


def fail(msg, hint=None):
    print(f"错误: {msg}", file=sys.stderr)
    if hint:
        print(f"提示: {hint}", file=sys.stderr)
    sys.exit(1)


def hr():
    info("-" * 46)


# ---------------------------------------------------------------- HTTP

class Api:
    def __init__(self, cookie=None, jar=None):
        self.cookie = cookie
        self.jar = jar
        if cookie:
            self.opener = urllib.request.build_opener()
        else:
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("User-Agent", UA)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        if data:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=25) as resp:
                return json.loads(resp.read().decode()), resp.headers.get("Date")
        except urllib.error.HTTPError as e:
            fail(f"HTTP {e.code}", e.read().decode(errors="replace")[:200])
        except urllib.error.URLError as e:
            fail(f"网络不通: {e.reason}", "检查网络或代理设置")


def load_jar():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(str(JAR_PATH))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (FileNotFoundError, http.cookiejar.LoadError):
        pass
    return jar


def save_jar(jar):
    if jar is None:
        return
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(JAR_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- 凭证

def session_id_from_device():
    """从小程序存储里读 sessionId，需要 adb + root。"""
    if not shutil.which("adb"):
        return None
    cmd = (
        "su -c 'cat /data/data/com.tencent.mm/files/mmkv/AppBrandMMKVStorage* "
        "2>/dev/null'"
    )
    try:
        out = subprocess.run(
            ["adb", "shell", cmd], capture_output=True, timeout=60
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(
        APPID.encode()
        + rb"__sessionId.{0,40}?"
        + rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        out,
        re.S,
    )
    return m.group(1).decode() if m else None


def normalize_cookie(text):
    text = text.strip().strip("'\"")
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if not text:
        return None
    if "=" not in text:
        text = "SESSION=" + text
    return text


def normalize_session_id(text):
    text = text.strip().strip("'\"")
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        text,
        re.I,
    )
    return m.group(0) if m else None


def try_saved_login():
    """复用上次缓存的会话；无效则返回 None。"""
    if not JAR_PATH.exists():
        return None
    jar = load_jar()
    api = Api(jar=jar)
    try:
        res, date = api.call("GET", "/auth/user-info")
    except SystemExit:
        return None
    if res.get("code") == 0:
        save_jar(jar)
        return api, res, date
    return None


def ask(prompt, default=""):
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        info()
        sys.exit(130)
    return value or default


def interactive_login():
    info("「晚安成电」签到")
    info()
    info("请选择登录方式：")
    info("  1. 从手机读取（需要 adb 连接 + 手机 root）")
    info("  2. 粘贴浏览器 Cookie（电脑已登录 jzsz.uestc.edu.cn）")
    info("  3. 粘贴 sessionId（小程序 H5 地址栏里的参数）")
    info()

    while True:
        choice = ask("输入 1/2/3: ")
        if choice == "1":
            info("正在读取手机…")
            sid = session_id_from_device()
            if not sid:
                info("没读到 sessionId。确认 adb 已连接、手机已 root、小程序打开过打卡页。")
                continue
            return Api(jar=load_jar()), "/auth/user-info?sessionId=" + sid

        if choice == "2":
            cookie = normalize_cookie(ask("粘贴 Cookie（SESSION=...）: "))
            if not cookie:
                info("内容为空，重试。")
                continue
            return Api(cookie=cookie), "/auth/user-info"

        if choice == "3":
            cookie = normalize_session_id(ask("粘贴 sessionId: "))
            if not cookie:
                info("不像是 uuid，重试。")
                continue
            return Api(jar=load_jar()), "/auth/user-info?sessionId=" + cookie

        info("请输入 1、2 或 3。")


# ---------------------------------------------------------------- 主流程

def server_now(date_header):
    if not date_header:
        return datetime.now(CST)
    try:
        return datetime.strptime(
            date_header, "%a, %d %b %Y %H:%M:%S %Z"
        ).replace(tzinfo=timezone.utc).astimezone(CST)
    except ValueError:
        return datetime.now(CST)


def explain(msg):
    table = {
        "未登录或会话已失效": "登录态过期，重新登录一次（--cookie / --session-id / 交互选择）",
        "当前不在打卡时间段内": "打卡窗口由服务端控制，到点再跑",
        "今日已完成打卡": "今天已经打过卡了，无需重复",
        "定位经纬度不能为空": "没有可用打卡点，或没有传坐标",
        "未获取到您的宿舍信息": "账号没有绑定宿舍，找辅导员处理",
    }
    for key, hint in table.items():
        if key in msg:
            return hint
    return None


def main():
    ap = argparse.ArgumentParser(
        description="「晚安成电」签到（交互式，无依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python3 checkin.py
  python3 checkin.py --yes
  python3 checkin.py --cookie "SESSION=xxxx" --yes
  python3 checkin.py --from-device --dry-run
""",
    )
    ap.add_argument("--session-id")
    ap.add_argument("--from-device", action="store_true")
    ap.add_argument("--cookie")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--accuracy", type=float, default=18.0)
    ap.add_argument("--yes", action="store_true", help="跳过确认直接提交")
    ap.add_argument("--dry-run", action="store_true", help="只看状态，不提交")
    ap.add_argument("--logout", action="store_true", help="清除本地缓存")
    a = ap.parse_args()

    if a.logout:
        try:
            JAR_PATH.unlink()
            info("已清除本地会话缓存。")
        except FileNotFoundError:
            info("没有缓存的会话。")
        return

    # 1. 拿凭证
    api = res = date = None
    if a.cookie:
        api = Api(cookie=normalize_cookie(a.cookie))
        path = "/auth/user-info"
    elif a.session_id:
        api = Api(jar=load_jar())
        path = "/auth/user-info?sessionId=" + urllib.parse.quote(a.session_id)
    elif a.from_device:
        sid = session_id_from_device()
        if not sid:
            fail("没从手机读到 sessionId", "确认 adb 已连接、手机已 root、小程序打开过打卡页")
        api = Api(jar=load_jar())
        path = "/auth/user-info?sessionId=" + sid
    else:
        saved = try_saved_login()
        if saved:
            api, res, date = saved
            info("复用上次的登录态。")
        elif sys.stdin.isatty():
            api, path = interactive_login()
        else:
            fail(
                "没有登录凭证",
                "用 --cookie / --session-id / --from-device，或直接运行进入交互模式",
            )

    # 2. 登录并显示账号
    if res is None:
        res, date = api.call("GET", path)
    if res.get("code") != 0:
        msg = res.get("msg", "")
        fail(f"登录失败: {msg}", explain(msg))

    user = res.get("data", {}).get("userData", {})
    now = server_now(date)
    info(f"账号: {user.get('fullName', '?')}")
    info(f"服务器时间: {now:%Y-%m-%d %H:%M:%S} UTC+8")

    # 3. 今日状态
    today, _ = api.call("GET", "/student-checkin-record/today")
    rec = today.get("data") or {}
    if not rec:
        fail("拿不到今日记录", "账号可能不在签到名单里")

    status = rec.get("recordStatusCode")
    if status == "CHECKED_IN":
        info(f"今日已打卡: {rec.get('checkinTime')}（距离 {rec.get('distanceMeter')} m）")
        save_jar(api.jar)
        return
    if status in REPORTED:
        fail("今天已提交情况说明，不能再打卡", "如需修改，联系辅导员")

    # 4. 时段
    sched, _ = api.call("GET", "/checkin-schedule/effective")
    s = sched.get("data") or {}
    try:
        day = datetime.strptime(rec["businessDate"], "%Y-%m-%d").replace(tzinfo=CST)
        start = day + timedelta(minutes=s["studentCheckinStartOffsetMinute"])
        end = day + timedelta(minutes=s["studentCheckinEndOffsetMinute"])
    except (KeyError, ValueError):
        fail("打卡时段数据异常", "稍后重试或联系管理员")

    info(f"打卡窗口: {start:%H:%M} - {end:%H:%M}")
    if not (start <= now <= end):
        fail(
            "当前不在打卡时段",
            f"窗口是 {start:%Y-%m-%d %H:%M} 到 {end:%H:%M}（服务器时间）",
        )

    # 5. 打卡点
    locs, _ = api.call("GET", "/checkin-location/available-locations")
    loc = (locs.get("data") or [None])[0]
    if not loc and (a.lat is None or a.lon is None):
        fail("没有可用打卡点", "用 --lat/--lon 手动指定坐标")

    lat = a.lat if a.lat is not None else loc["latitude"]
    lon = a.lon if a.lon is not None else loc["longitude"]

    info(f"打卡点: {loc['locationName']}（半径 {loc.get('radiusMeter')} m）" if loc else "打卡点: 自定义")
    info(f"提交坐标: {lat}, {lon}")

    if a.dry_run:
        info("dry-run：不提交。")
        return

    # 6. 提交
    if not a.yes:
        if not sys.stdin.isatty():
            info("非交互环境需要加 --yes 才会提交。")
            return
        if ask("确认提交打卡？[y/N] ").lower() not in ("y", "yes"):
            info("已取消。")
            return

    body = {
        "latitude": lat,
        "longitude": lon,
        "reverseGeocode": "",
        "clientTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "userAgent": UA,
        "locationDurationMs": 1500,
        "accuracy": a.accuracy,
        "networkTypeCode": "wifi",
        "locationEnabled": True,
        "locationAuthorized": "authorized",
        "locationAccuracy": "full",
    }
    out, _ = api.call("POST", "/student-checkin-record/checkin", body)
    if out.get("code") != 0:
        msg = out.get("msg", "")
        fail(f"提交失败: {msg}", explain(msg))

    after, _ = api.call("GET", "/student-checkin-record/today")
    d = after.get("data") or {}
    save_jar(api.jar)

    hr()
    info("打卡成功")
    info(f"  状态: {d.get('recordStatusCode')}")
    info(f"  时间: {d.get('checkinTime')}")
    info(f"  距离: {d.get('distanceMeter')} m")
    info(f"  异常: {'是' if d.get('abnormalFlag') else '否'}")


if __name__ == "__main__":
    main()
