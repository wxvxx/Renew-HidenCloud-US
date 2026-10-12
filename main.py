#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import random
import requests
import subprocess
import contextlib
from seleniumbase import SB

# ---------- 环境变量 ----------
EMAIL        = os.getenv("EMAIL") or ""  # 登录邮箱（也是 TG 通知里显示的账号）
PASSWORD     = os.getenv("PASSWORD") or ""      # 登录密码 
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN") or ""
TG_CHAT_ID   = os.getenv("TG_CHAT_ID") or ""

BASE_URL  = "https://dash.hidencloud.com/"
LOGIN_URL = BASE_URL + "auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"

# ---------- Telegram 通知 ----------
def send_telegram_text(text) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                             timeout=10)
        if resp.status_code == 200:
            print("✅ Telegram 通知发送成功")
            return True
        print(f"❌ Telegram 通知失败: {resp.text}")
    except Exception as e:
        print(f"❌ Telegram 通知异常: {e}")
    return False

def send_telegram_notification(status, old_due, new_due) -> bool:
    """续期结果通知"""
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else EMAIL
    else:
        masked = EMAIL[:2] + '****'
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    return send_telegram_text(
        f"🇺🇸 HidenCloud 续期通知\n\n{status}\n"
        f"👤 账号: {masked}\n📅 续期前到期：{old_due}\n📅 续期后到期：{new_due}\n🕒 续期时间：{now}")

# ---------- 常用选择器（CDP 模式：CSS + :contains("文本")）----------
USERNAME_SEL = 'input[name="username"], input[name="email"]'
PASSWORD_SEL = 'input[name="password"]'
SUBMIT_SEL   = 'button[type="submit"]'
RENEW_BTN_SELECTORS = [
    'button[onclick*="showRenewAlert"]',             # 新版 Renew 按钮（onclick 弹窗）
    'button[data-modal-target*="renewService"]',     # 旧版 Renew 按钮（Flowbite 弹窗）
    'button:contains("Renew")',                      # 文本兜底
]
CREATE_INVOICE_SELECTORS = [
    'form[id^="renew-form"] button[type="submit"]',  # 弹窗表单里的 Create Invoice
    'button:contains("Create Invoice")',
    '.swal2-confirm',                                # 若弹窗改成了 SweetAlert
]
MY_ACCOUNT_SELECTORS = [
    'a:contains("My Account")',
    'button:contains("My Account")',
]
PAY_SELECTORS = [
    'button:contains("Pay Now")',
    'a:contains("Pay Now")',
    'button:contains("Pay")',
    'a:contains("Pay")',
    '.swal2-confirm',
]

# 页面是否卡在 Cloudflare 验证的特征（标题 + 页面文本）
CF_INDICATORS = ["just a moment", "请稍候", "security verification", "protection enabled",
                 "verify you are human", "确认你是真人", "troubleshoot"]

# Cookie 同意窗口（全新浏览器每次都会弹，不点 Accept 会盖住 Renew/弹窗/按钮）
CONSENT_MARKERS = ["control your privacy", "advanced settings"]
CONSENT_ACCEPT_SELECTORS = [
    'button:contains("Accept")',
    'a:contains("Accept")',
    '#didomi-notice-agree-button',      # Didomi
    '#onetrust-accept-btn-handler',     # OneTrust
    '#cmpbntyestxt',                    # consentmanager.net
]

# ---------- JS 片段（都返回字符串，避免 CDP 序列化问题）----------
SCRIPTS_READY_JS = """
return (document.readyState === 'complete' ||
        typeof window.Flowbite !== 'undefined' ||
        typeof window.flowbite !== 'undefined') ? '1' : '0';
"""

TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

# 页面脚本没就绪时点 Renew 没反应，兜底用 JS 直接触发 Renew 按钮自身的点击逻辑
CLICK_RENEW_BY_DOM_JS = """
var buttons = document.querySelectorAll('button');
for (var i = 0; i < buttons.length; i++) {
    var b = buttons[i];
    var text = (b.innerText || '').trim();
    var oc = b.getAttribute('onclick') || '';
    if (text.indexOf('Renew') >= 0 &&
            (oc.indexOf('showRenewAlert') >= 0 || b.getAttribute('data-modal-target'))) {
        b.click();
        return '1';
    }
}
return '0';
"""

# ---------- 基础工具 ----------
def js(sb, script, default=None):
    """执行 JS（脚本用 return 返回），失败时改走 cdp.evaluate，再失败返回 default"""
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception:
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception:
            return default

def body_text(sb) -> str:
    text = js(sb, "return document.body ? document.body.innerText : ''", "")
    return text if isinstance(text, str) else ""

def shot(sb, name):
    """失败时截图，截图本身失败不影响主流程"""
    try:
        sb.save_screenshot(name)
    except Exception:
        pass

def wait_first_visible(sb, selectors, timeout=10) -> bool:
    """selectors 里任意一个可见就返回 True"""
    deadline = time.time() + timeout
    while True:
        for sel in selectors:
            try:
                if sb.is_element_visible(sel):
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)

def click_first_visible(sb, selectors, timeout=10, tag="") -> bool:
    """点击第一个能点的元素：先直接 sb.click（自带等待），不行再用 JS 点"""
    for sel in selectors:
        try:
            sb.click(sel, timeout=min(timeout, 8))
            print(f"🖱️ 已点击 {tag or sel}")
            return True
        except Exception:
            continue
    for sel in selectors:
        if not sel.startswith("//"):
            try:
                if js(sb, 'var e = document.querySelector("%s");'
                          'if (e) { e.click(); return "1"; } return "0";' % sel, "0") == "1":
                    print(f"🖱️ 已用 JS 点击 {tag or sel}")
                    return True
            except Exception:
                continue
    return False

def close_cookie_consent(sb) -> bool:
    """关掉 Cookie 同意窗口（不点 Accept 会盖住 Renew 按钮/弹窗，导致定位和点击全部失效）"""
    try:
        src = (sb.get_page_source() or "").lower()
    except Exception:
        return False
    if not any(x in src for x in CONSENT_MARKERS):
        return False
    for sel in CONSENT_ACCEPT_SELECTORS:
        try:
            sb.click(sel, timeout=3)
            print("🍪 已点击 Accept 关闭 Cookie 同意窗口")
            time.sleep(1)
            return True
        except Exception:
            continue
    print("⚠️ 检测到 Cookie 同意窗口但没点掉")
    return False

def renew_modal_open(sb) -> bool:
    r = js(sb, """
        var f = document.querySelector('form[id^="renew-form"]');
        if (!f) return '0';
        var rect = f.getBoundingClientRect();
        return (rect.width > 100 && rect.height > 100) ? '1' : '0';
    """, "0")
    return r == "1"

def cf_challenge_now(sb) -> bool:
    """当前是否卡在 Cloudflare 验证页/验证组件"""
    try:
        title = (sb.get_title() or "").lower()
        if any(x in title for x in ("just a moment", "请稍候")):
            return True
        src = (sb.get_page_source() or "").lower()
        return any(x in src for x in CF_INDICATORS)
    except Exception:
        return False


def wait_page_scripts(sb, timeout=30) -> bool:
    deadline = time.time() + timeout
    ready = "0"
    while time.time() < deadline:
        ready = js(sb, SCRIPTS_READY_JS, "0")
        if ready == "1":
            print("✅ 页面脚本已就绪")
            return True
        time.sleep(2)
    print(f"⚠️ 页面脚本等待超时（ready={ready}），继续尝试")
    return False

# ---------- Turnstile 验证 ----------
def _xdotool_click(x: int, y: int):
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"],
                       capture_output=True, text=True, timeout=5)
    except Exception:
        try:
            import pyautogui
            pyautogui.click(x, y)
        except Exception as e:
            print(f"⚠️ 底层点击失败: {e}")

def _click_turnstile(sb):
    """兜底：JS 算出验证框复选框的屏幕坐标，用系统级点击"""
    coords = js(sb, """
        var frames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
        for (var i = 0; i < frames.length; i++) {
            var r = frames[i].getBoundingClientRect();
            if (r.width > 50 && r.height > 20)
                return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
        }
        var box = document.querySelector('.cf-turnstile');
        if (box) {
            var r = box.getBoundingClientRect();
            if (r.width > 50 && r.height > 20)
                return {cx: Math.round(r.x + 27), cy: Math.round(r.y + 29)};
        }
        return null;
    """)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标")
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def turnstile_token(sb) -> str:
    """读取 Turnstile token（官方 JS 接口 或 表单隐藏域），未通过时为空串"""
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def wait_for_turnstile_pass(sb, timeout=30) -> bool:
    """等待验证通过：先看 token，再看页面是否还卡在验证上"""
    start = time.time()
    while time.time() - start < timeout:
        if turnstile_token(sb):
            print("✅ Turnstile 验证已通过（已获取 token）")
            return True
        if not cf_challenge_now(sb):
            print("✅ Turnstile 验证已通过")
            return True
        time.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False

def turnstile_click_point(sb):
    """验证框复选框的视口坐标（复选框在组件左侧垂直居中处），返回 "x|y" 或 "" """
    return js(sb, """
        var frames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
        for (var i = 0; i < frames.length; i++) {
            var r = frames[i].getBoundingClientRect();
            if (r.width > 50 && r.height > 20)
                return Math.round(r.x + 30) + '|' + Math.round(r.y + r.height / 2);
        }
        var box = document.querySelector('.cf-turnstile');
        if (box) {
            var r = box.getBoundingClientRect();
            if (r.width > 50 && r.height > 20)
                return Math.round(r.x + 27) + '|' + Math.round(r.y + 29);
        }
        return '';
    """, "")

def click_turnstile(sb):
    if _click_turnstile(sb):
        return True
    point = turnstile_click_point(sb)
    if point:
        try:
            cx, cy = point.split("|")
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ SB 坐标点击 Turnstile ({cx},{cy})")
                sb.cdp.gui_click_x_y(int(cx), int(cy))
                return True
        except Exception as e:
            print(f"⚠️ SB 坐标点击失败: {e}")
    try:
        sb.solve_captcha()
        return True
    except Exception as e:
        print(f"⚠️ solve_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8, reopen=None) -> bool:
    if turnstile_token(sb):
        return True
    print(f"🛡️ 处理 {tag}：先等 {first_wait} 秒自动验证...")
    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过（已获取 token）")
            return True
        if reopen and not renew_modal_open(sb):
            print("⚠️ 弹窗似乎被关掉了，重新打开后继续验证...")
            try:
                reopen()
            except Exception:
                pass
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

# ---------- 登录 ----------
def wait_dashboard_ready(sb, timeout=60) -> bool:
    """等 dashboard 加载出服务列表（/service/ 链接）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if "auth/login" not in (sb.get_current_url() or "") and \
                    "/service/" in (sb.get_page_source() or ""):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def get_server_id(sb):
    """从 dashboard 页面里获取 Server ID"""
    try:
        time.sleep(3)
        for attempt in range(2):
            html = sb.get_page_source() or ""
            print(f"📝 页面长度: {len(html)}, URL: {sb.get_current_url()}")

            matches = re.findall(r'/service/(\d+)/manage', html) or re.findall(r'/service/(\d+)', html)
            if matches:
                print(f"✅ 从链接中获取到 Server ID: {matches[0]}")
                return matches[0]

            matches = re.findall(r'#(\d{4,})', html)
            if matches:
                print(f"✅ 从文本 #号中获取到 Server ID: {matches[0]}")
                return matches[0]

            if attempt == 0:
                print("⚠️ 未找到 Server ID，处理验证后重试...")
                click_turnstile(sb)
                time.sleep(3)
        print("❌ 页面里没找到 Server ID")
        return None
    except Exception as e:
        print(f"❌ 获取 Server ID 失败: {e}")
        shot(sb, "server_id_error.png")
        return None

def get_due_date(sb, service_url=None):
    """读取服务当前到期时间"""
    try:
        if service_url and service_url not in (sb.get_current_url() or ""):
            sb.open(service_url)
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for attempt in range(2):
            text = body_text(sb)
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                if match:
                    print(f"📅 获取到Due Date: {match.group(1).strip()}")
                    return match.group(1).strip()
            if attempt == 0:
                print("⚠️ 未读取到 Due Date，处理验证后重试...")
                click_turnstile(sb)
                time.sleep(3)
    except Exception as e:
        print(f"❌ 获取Due Date失败: {e}")
    return "未知"

def click_renew_button(sb) -> bool:
    for sel in RENEW_BTN_SELECTORS:
        try:
            sb.click(sel, timeout=8)
            print(f"🖱️ 已点击 Renew（{sel}）")
            return True
        except Exception:
            continue
    if js(sb, CLICK_RENEW_BY_DOM_JS, "0") == "1":
        print("🖱️ 已用 JS 底层点击 Renew 按钮")
        return True
    return False

def open_renew_modal(sb, attempts=4):
    """点开续期弹窗，返回 True / False / 'NOT_TIME'"""
    for i in range(attempts):
        close_cookie_consent(sb)    # Cookie 窗口会盖住 Renew 和弹窗，先关掉
        print(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
        if not click_renew_button(sb):
            print("⚠️ 没点到 Renew 按钮，处理验证后重试...")
            click_turnstile(sb)

        time.sleep(2)
        text = body_text(sb)
        if "Renewal Restricted" in text or "can only renew" in text.lower():
            print("⚠️ 未到续期时间，无法续期。")
            shot(sb, "renew_not_allowed.png")
            return "NOT_TIME"

        print("🖲️ 等待弹窗出现...")
        if wait_until_modal_open(sb, timeout=15):
            print("✅ 弹窗已成功弹出！")
            return True
        print("⚠️ 弹窗没出现，等页面脚本就绪后重试...")
        wait_page_scripts(sb, timeout=10)
    return False

def wait_until_modal_open(sb, timeout=15) -> bool:
    """轮询等待续期弹窗真正展开"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if renew_modal_open(sb):
            return True
        time.sleep(1)
    return False

def renew_service(sb, service_url):
    """续期主流程，返回 True / False / 'NOT_TIME'"""
    try:
        print("➡ 进入续期流程...")
        if service_url not in (sb.get_current_url() or ""):
            sb.open(service_url)

        print("⏳ 等待服务页加载完成（Renew 是弹窗按钮，脚本没就绪时点了没反应）...")
        close_cookie_consent(sb)    # Cookie 窗口会盖住页面，先关掉
        wait_page_scripts(sb, timeout=30)
        sb.sleep(5)

        modal_opened = open_renew_modal(sb)
        if modal_opened != True:
            if modal_opened is False:
                print("❌ 尝试多次后，续费弹窗仍未出现。")
                shot(sb, "renew_modal_failed.png")
            return modal_opened

                # 弹窗内的 Turnstile：和登录界面同一套验证逻辑；弹窗被误关时自动重开
        if solve_turnstile(sb, timeout=150, tag="续期弹窗 Turnstile", first_wait=10,
                           reopen=lambda: open_renew_modal(sb)):
            print("✅ 弹窗 Turnstile 已通过")
        else:
            print("⚠️ 弹窗 Turnstile 未确认通过，仍尝试提交（失败请查看截图）")
            shot(sb, "turnstile_not_passed.png")

        print("🖱️ 点击 'Create Invoice'...")
        close_cookie_consent(sb)    # 防止 Cookie 窗口盖住弹窗按钮
        click_first_visible(sb, CREATE_INVOICE_SELECTORS, timeout=15, tag="Create Invoice")

        # 等待跳转发票页；若又出现验证，重新处理后补提交一次
        new_invoice_url = None
        start_wait = time.time()
        resubmitted = False
        while time.time() - start_wait < 90:
            current = sb.get_current_url() or ""
            if "/payment/invoice/" in current:
                new_invoice_url = current
                print(f"🎉 页面已跳转: {new_invoice_url}")
                break
            if not resubmitted and wait_first_visible(sb, CREATE_INVOICE_SELECTORS, 0.6) \
                    and not turnstile_token(sb):
                resubmitted = True
                print("⚠️ 提交后弹窗还在且验证未通过，重新处理...")
                solve_turnstile(sb, timeout=60, tag="续期弹窗 Turnstile", first_wait=5)
                click_first_visible(sb, CREATE_INVOICE_SELECTORS, timeout=15, tag="Create Invoice")
            time.sleep(1)

        if not new_invoice_url:
            print("❌ 没能进入发票页面，超时。")
            shot(sb, "renew_stuck_invoice.png")
            return False

        if (sb.get_current_url() or "") != new_invoice_url:
            sb.open(new_invoice_url)

        print("🔎 查找 'Pay' 按钮...")
        close_cookie_consent(sb)    # 发票页也可能弹 Cookie 窗口
        if not click_first_visible(sb, PAY_SELECTORS, timeout=30, tag="Pay"):
            print("❌ 没找到可点的 'Pay' 按钮")
            shot(sb, "pay_button_failed.png")
            return False
        print("✅ 'Pay' 按钮已点击。")

        time.sleep(5)
        sb.open(service_url)
        return True

    except Exception as e:
        print(f"❌ 续费异常: {e}")
        shot(sb, "renew_error.png")
        return False

# ---------- 主流程 ----------
@contextlib.contextmanager
def _quiet_sb(**kwargs):
    devnull = open(os.devnull, "w")
    with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
        sb_cm = SB(**kwargs)
        sb = sb_cm.__enter__()
    try:
        yield sb
    finally:
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            sb_cm.__exit__(None, None, None)
        devnull.close()

def main():
    print("=" * 25)
    print("   HidenCloud 自动续期")
    print("=" * 25)

    sb_kwargs = {"uc": True, "test": True, "headless": False, "locale": "en"}
    if IS_PROXY:
        print(f"🔗 挂载 Sing-box 代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🌐 未使用代理，直连访问")

    try:
        with _quiet_sb(**sb_kwargs) as sb:
            print("✅ 浏览器已启动")
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"🌐 当前出口真实 IP: {sb.get_text('body').strip()}")
            except Exception as e:
                print(f"⚠️ 出口 IP 检测失败: {e}")

            # ----- 第一阶段：过登录页前的 Cloudflare 安全验证 -----
            print("🌐 正在访问目标网站...")
            sb.activate_cdp_mode(LOGIN_URL)
            sb.sleep(6)
            sb.solve_captcha()
            time.sleep(5)

            deadline = time.time() + 150
            clicks = 0
            passed = False
            while time.time() < deadline:
                if wait_first_visible(sb, [USERNAME_SEL, PASSWORD_SEL], 0.6):
                    print("✅ 安全验证已通过，登录表单已出现")
                    passed = True
                    break
                if cf_challenge_now(sb) or "challenge" in (sb.get_current_url() or ""):
                    clicks += 1
                    print(f"🛡️ [第 {clicks} 次] 检测到 Cloudflare 验证，点击验证框...")
                    click_turnstile(sb)
                    time.sleep(6)
                else:
                    time.sleep(3)
            if not passed:
                # 兜底：底层坐标点击
                for _ in range(3):
                    _click_turnstile(sb)
                    if wait_for_turnstile_pass(sb, timeout=15):
                        passed = True
                        break
            if not passed:
                shot(sb, "turnstile_failed.png")
                print("❌ 安全验证始终未通过，退出。")
                send_telegram_text("❌ HidenCloud 续期失败：安全验证未通过")
                sys.exit(1)

            # ----- 第二阶段：登录 -----
            current_url = sb.get_current_url()
            print(f"📄 当前URL: {current_url}")

            if "auth/login" in (sb.get_current_url() or ""):
                sb.sleep(10)
                sb.type(USERNAME_SEL, EMAIL)
                sb.type(PASSWORD_SEL, PASSWORD)
                print("📝 已填写账号密码")
                sb.sleep(2)
                # 登录表单上的 Turnstile（先等自动验证，没过就点）
                sb.solve_captcha()
                sb.sleep(2)
                if not wait_for_turnstile_pass(sb, timeout=15):
                    print("⚠️ 第一次点击后未通过，用坐标兜底再点一次...")
                    _click_turnstile(sb)
                    if not wait_for_turnstile_pass(sb, timeout=15):
                        print("❌ 两次点击后仍未通过，登录失败")
                        shot(sb, "printin_turnstile_failed.png")
                        sys.exit(1)
                sb.sleep(2)
                print("🖱️ 点击登录按钮提交登录...")
                sb.click(SUBMIT_SEL)
                sb.sleep(6)
                sb.open(BASE_URL + "dashboard")
                sb.sleep(6)
                if "/auth/login" in (sb.get_current_url() or ""):
                    print("❌ 登录失败，仍停留在登录页")
                    shot(sb, "printin_fail.png")
                    sys.exit(1)
                print(f"✅ 账号密码登录成功！当前URL: {sb.get_current_url()}")
                if not wait_dashboard_ready(sb, timeout=60):
                    print("⚠️ dashboard 服务列表还没加载出来，继续尝试...")

            # ----- 第三阶段：执行续期 -----
            sb.sleep(2)
            server_id = get_server_id(sb)
            if not server_id:
                print("❌ 无法获取 Server ID，退出。")
                send_telegram_notification("❌ 续期失败", "未找到 Server ID", "-")
                sys.exit(1)

            service_url = f"{BASE_URL}service/{server_id}/manage"
            old_due = get_due_date(sb, service_url)
            print(f"📆 续费前到期时间：{old_due}")

            renew_result = renew_service(sb, service_url)
            new_due = old_due
            if renew_result == "NOT_TIME":
                # 站点弹窗会提示剩余天数
                extra = ""
                m = re.search(r"expires in\s+(\d+)\s+day", body_text(sb), re.IGNORECASE)
                if m:
                    extra = f"(服务将在{m.group(1)}天后到期，小于1天才能续期)"
                print(f"⏳ 未到续期时间，目前无法续期{extra}")
                status = f"⏳ 未到续期时间{extra}"
            elif renew_result is False:
                print("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:
                new_due = get_due_date(sb, service_url)
                print(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due)
            print("✅ 所有任务完成。")
            sys.exit(0 if renew_result is not False else 1)

    except Exception as e:
        import traceback
        print(f"❌ 脚本异常: {e}")
        traceback.print_exc()
        send_telegram_text(f"❌ HidenCloud 续期脚本异常: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
