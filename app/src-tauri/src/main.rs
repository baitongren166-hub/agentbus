// agentbus 桌面应用：
//   悬浮窗（hud）— 挂在屏幕顶部或侧边、永远置顶，派活时自己弹出来；
//   主窗口（main）— 基地 / 队员 / 技能 / 任务 / 战报 / 设置，平时藏着，从悬浮窗或菜单栏打开。
// 界面和数据都在本地服务（core/service.py，127.0.0.1:8790）里，Claude 右侧浏览器打开同一个地址。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, LogicalPosition, LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const ADDR: &str = "127.0.0.1:8790";
const URL: &str = "http://127.0.0.1:8790/";

fn http_get(path: &str) -> Option<String> {
    let mut s = TcpStream::connect_timeout(&ADDR.parse().ok()?, Duration::from_millis(400)).ok()?;
    // 英特尔 Mac 上打包版服务刚起来时很慢：读超时给宽一点，慢不等于挂了
    s.set_read_timeout(Some(Duration::from_secs(15))).ok()?;
    write!(s, "GET {path} HTTP/1.0\r\nHost: {ADDR}\r\n\r\n").ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    buf.split_once("\r\n\r\n").map(|(_, b)| b.to_string())
}

/// 服务端口有没有人在听：只看连不连得上，不等回应（服务忙、慢都算活着）
fn service_listening() -> bool {
    ADDR.parse().ok().and_then(|a| TcpStream::connect_timeout(&a, Duration::from_millis(400)).ok()).is_some()
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// 应用自己的日志（临时目录 agentbus-app.log）：服务掉线、拉起、悬浮窗重载都记一笔，悬浮窗出问题时看这里
fn applog(msg: &str) {
    let f = std::env::temp_dir().join("agentbus-app.log");
    if std::fs::metadata(&f).map(|m| m.len() > 512 * 1024).unwrap_or(false) {
        let _ = std::fs::remove_file(&f);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(f) {
        let _ = writeln!(f, "{} {msg}", now_secs());
    }
}

/// 应用拉起的那个服务进程：它还活着（可能还在启动）就不再拉第二个
static SPAWNED: Mutex<Option<Child>> = Mutex::new(None);
/// 悬浮窗网页最近一次报平安的时间（秒）；0 = 还没报过
static HUD_BEAT: AtomicU64 = AtomicU64::new(0);

/// 打包好的服务程序（PyInstaller 文件夹版，作为应用资源放在 core-bin/ 里）。发布版用它，用户不用装 Python。
fn sidecar(app: &AppHandle) -> Option<PathBuf> {
    let name = if cfg!(windows) { "agentbus-core.exe" } else { "agentbus-core" };
    let p = app.path().resource_dir().ok()?.join("core-bin").join(name);
    p.exists().then_some(p)
}

/// 找 core/service.py：先看环境变量，再看 ~/agentbus/core，最后看应用自带的资源。
fn service_script(app: &AppHandle) -> Option<PathBuf> {
    let mut cands = vec![];
    if let Ok(p) = std::env::var("AGENTBUS_CORE") {
        cands.push(PathBuf::from(p).join("service.py"));
    }
    if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
        cands.push(PathBuf::from(home).join("agentbus").join("core").join("service.py"));
    }
    if let Ok(res) = app.path().resource_dir() {
        cands.push(res.join("core").join("service.py"));
    }
    cands.into_iter().find(|p| p.exists())
}

/// 用哪个 Python 跑源码版服务：Mac 用系统自带的；Windows 先试 py 启动器，再试 python。
fn python_cmd() -> Command {
    if cfg!(windows) {
        for (exe, pre) in [("py", vec!["-3"]), ("python", vec![]), ("python3", vec![])] {
            if Command::new(exe).args(&pre).arg("--version").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
                let mut c = Command::new(exe);
                c.args(pre);
                return c;
            }
        }
        Command::new("python")
    } else {
        Command::new("/usr/bin/python3")
    }
}

#[cfg(windows)]
fn no_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW：别弹黑框
}
#[cfg(not(windows))]
fn no_window(_cmd: &mut Command) {}

fn service_is_older(body: &str, app_version: &str) -> bool {
    let Ok(health) = serde_json::from_str::<serde_json::Value>(body) else { return false };
    if health["app"].as_str() != Some("agentbus") {
        return false;
    }
    let installed = health["version"].as_str().unwrap_or("0.0.0");
    let parts = |version: &str| version.split('.').map(str::parse::<u64>).collect::<Result<Vec<_>, _>>();
    matches!((parts(installed), parts(app_version)), (Ok(old), Ok(new)) if old < new)
}

fn refresh_bundled_service(app: &AppHandle) {
    if std::env::var_os("AGENTBUS_CORE").is_some() || sidecar(app).is_none() {
        return;
    }
    let Some(health) = http_get("/api/health") else { return };
    if service_is_older(&health, &app.package_info().version.to_string()) {
        let _ = http_get("/api/shutdown");
        for _ in 0..25 {
            std::thread::sleep(Duration::from_millis(200));
            if http_get("/api/health").is_none() {
                break;
            }
        }
    }
}

/// 服务独立运行，供应用和命令行共用。
fn ensure_service(app: &AppHandle) {
    if service_listening() {
        return;
    }
    // 上次拉起的服务还在跑（英特尔 Mac 上打包版第一次启动要好几十秒）：等它，别再拉一个跟它抢端口
    let mut spawned = SPAWNED.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(c) = spawned.as_mut() {
        if matches!(c.try_wait(), Ok(None)) {
            return;
        }
    }
    let dev = std::env::var_os("AGENTBUS_CORE").map(|p| PathBuf::from(p).join("service.py")).filter(|p| p.is_file());
    let mut cmd = if let Some(script) = dev {
        let mut c = python_cmd();
        c.arg(&script).arg("8790");
        c
    } else if let Some(bin) = sidecar(app) {
        let mut c = Command::new(bin);
        c.args(["serve", "8790"]);
        if let Ok(res) = app.path().resource_dir() {
            c.env("AGENTBUS_UI", res.join("ui")); // 界面文件作为应用资源放着
        }
        c
    } else if let Some(script) = service_script(app) {
        let mut c = python_cmd();
        c.arg(&script).arg("8790");
        c
    } else {
        return;
    };
    // 开机启动要知道应用自己在哪
    if let Ok(exe) = std::env::current_exe() {
        cmd.env("AGENTBUS_APP_EXE", exe);
    }
    cmd.stdin(Stdio::null());
    no_window(&mut cmd);
    let log = std::env::temp_dir().join("agentbus-service.log");
    if let Ok(f) = std::fs::OpenOptions::new().create(true).append(true).open(&log) {
        if let Ok(f2) = f.try_clone() {
            cmd.stdout(f).stderr(f2);
        }
    }
    match cmd.spawn() {
        Ok(c) => {
            applog(&format!("拉起本地服务 pid={}", c.id()));
            *spawned = Some(c);
        }
        Err(e) => applog(&format!("拉起本地服务失败：{e}")),
    }
}

/// 打开主窗口：关掉时窗口是真的销毁了（网页进程攥着的立绘图层才会还给系统，藏起来不还），要用时再新建。
fn show_main(app: &AppHandle, page: Option<&str>) {
    let hash = page.map(|p| format!("#{}", p.replace('\'', "").trim_start_matches('#'))).unwrap_or_default();
    if let Some(w) = app.get_webview_window("main") {
        if !hash.is_empty() {
            let _ = w.eval(&format!("location.hash = '{}'", hash));
        }
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let url: tauri::Url = format!("{URL}{hash}").parse().unwrap();
    if let Ok(w) = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
        .title("agentbus")
        .inner_size(1360.0, 900.0)
        .min_inner_size(420.0, 560.0)
        .build()
    {
        let _ = w.set_focus();
    }
}

/// 悬浮窗大小变了：按停靠位置重新摆。
/// top = 菜单栏下方，默认水平居中；right / left = 屏幕侧边，默认以收起时的胶囊为准垂直居中。
/// offset = 用户拖动后的偏移（顶部左右、侧边上下）；anchor = 侧边收起时窗口的高度。
/// 悬浮窗所在屏幕的可用区域（逻辑像素）：x, y, 宽, 高
fn work_area(win: &tauri::WebviewWindow) -> Option<(f64, f64, f64, f64)> {
    let mon = win.current_monitor().ok().flatten().or_else(|| win.primary_monitor().ok().flatten())?;
    let s = mon.scale_factor();
    let a = mon.work_area();
    Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s))
}

/// 悬浮窗现在的位置和大小（拖动开始时用）
#[tauri::command]
fn hud_pos(app: AppHandle) -> Option<(f64, f64, f64, f64)> {
    let win = app.get_webview_window("hud")?;
    let s = win.scale_factor().ok()?;
    let p = win.outer_position().ok()?;
    let z = win.outer_size().ok()?;
    Some((p.x as f64 / s, p.y as f64 / s, z.width as f64 / s, z.height as f64 / s))
}

/// 拖动中：直接把窗口挪到 (x, y)
#[tauri::command]
fn hud_place(app: AppHandle, x: f64, y: f64) {
    if let Some(win) = app.get_webview_window("hud") {
        // 拖动时不让窗口出屏幕：出去了就找不到它在哪块屏幕上，之后再也摆不回来
        let (x, y) = match (hud_monitor(&win), win.outer_size()) {
            (Some((ax, ay, aw, ah, s)), Ok(z)) => {
                let (w, h) = (z.width as f64 / s, z.height as f64 / s);
                (x.max(ax).min(ax + aw - w.min(aw)), y.max(ay).min(ay + ah - h.min(ah)))
            }
            _ => (x, y),
        };
        let _ = win.set_position(LogicalPosition::new(x, y));
    }
}

/// 悬浮窗所在屏幕的可用区域（逻辑像素）+ 缩放；窗口跑到屏幕外时 current_monitor 是空的，退回主屏幕
fn hud_monitor(win: &tauri::WebviewWindow) -> Option<(f64, f64, f64, f64, f64)> {
    let mon = win.current_monitor().ok().flatten().or_else(|| win.primary_monitor().ok().flatten())?;
    let s = mon.scale_factor();
    let a = mon.work_area();
    Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s, s))
}

/// 松手时决定停在哪：离某条屏幕边不到 48 像素就吸附过去（返回边和沿边的偏移），否则就地悬浮（返回中心点）
#[tauri::command]
fn hud_snap(app: AppHandle, x: f64, y: f64, w: f64, h: f64) -> Option<(String, f64, f64, f64)> {
    let win = app.get_webview_window("hud")?;
    let (ax, ay, aw, ah) = work_area(&win)?;
    let d = [("left", x - ax), ("right", ax + aw - (x + w)), ("top", y - ay), ("bottom", ay + ah - (y + h))];
    let (edge, dist) = d.iter().cloned().fold(("", f64::MAX), |m, e| if e.1 < m.1 { e } else { m });
    let (cx, cy) = (x + w / 2.0, y + h / 2.0);
    if dist < 48.0 {
        let off = if edge == "top" || edge == "bottom" { cx - (ax + aw / 2.0) } else { cy - (ay + ah / 2.0) };
        return Some((edge.to_string(), off, cx, cy));
    }
    Some(("float".into(), 0.0, cx, cy))
}

/// 指针所在那块屏幕的可用区域（拖动跨屏时用）：找不到就退回悬浮窗当前所在的屏幕
fn monitor_at(win: &tauri::WebviewWindow, px: f64, py: f64) -> Option<(f64, f64, f64, f64, f64)> {
    let mons = win.available_monitors().ok()?;
    for mon in mons {
        let s = mon.scale_factor();
        let (p, z) = (mon.position(), mon.size());
        let (x, y, w, h) = (p.x as f64 / s, p.y as f64 / s, z.width as f64 / s, z.height as f64 / s);
        if px >= x && px < x + w && py >= y && py < y + h {
            let a = mon.work_area();
            return Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s, s));
        }
    }
    None
}

#[tauri::command]
fn hud_resize(app: AppHandle, w: f64, h: f64, dock: String, offset: Option<f64>, anchor: Option<f64>, cx: Option<f64>, cy: Option<f64>) {
    let Some(win) = app.get_webview_window("hud") else { return };
    // 拖动时带了指针位置：按指针所在的屏幕摆（能拖到第二块屏幕上）；平时按悬浮窗当前所在的屏幕
    let at = match (cx, cy) { (Some(x), Some(y)) => monitor_at(&win, x, y), _ => None };
    let Some((ax, ay, aw, ah, _)) = at.or_else(|| hud_monitor(&win)) else { return };
    let off = offset.unwrap_or(0.0);
    let h = h.min(ah - 16.0);
    let clamp = |v: f64, lo: f64, hi: f64| v.max(lo).min(hi.max(lo));
    let (x, y) = match dock.as_str() {
        "right" | "left" => {
            // 以收起时胶囊的中心为准上下对称展开，收起再缩回同一个中心
            let _ = anchor;
            let top = clamp(ay + ah / 2.0 + off - h / 2.0, ay + 8.0, ay + ah - h - 8.0);
            (if dock == "right" { ax + aw - w } else { ax }, top)
        }
        // 自由悬浮：以收起时胶囊的中心为准；在屏幕上半边往下展开，下半边往上展开
        "float" => {
            let (fx, fy) = (cx.unwrap_or(ax + aw / 2.0), cy.unwrap_or(ay + 80.0));
            let ph = anchor.unwrap_or(h);
            let top = if fy < ay + ah / 2.0 { fy - ph / 2.0 } else { fy + ph / 2.0 - h };
            (clamp(fx - w / 2.0, ax + 4.0, ax + aw - w - 4.0), clamp(top, ay + 4.0, ay + ah - h - 4.0))
        }
        // 底部：任务栏上方（Windows 常用）；work_area 已经扣掉任务栏
        "bottom" => (clamp(ax + (aw - w) / 2.0 + off, ax + 4.0, ax + aw - w - 4.0), ay + ah - h),
        _ => (clamp(ax + (aw - w) / 2.0 + off, ax + 4.0, ax + aw - w - 4.0), ay),
    };
    let _ = win.set_size(LogicalSize::new(w, h));
    let _ = win.set_position(LogicalPosition::new(x, y));
}

/// 自动弹出时拉到最上层（不抢键盘焦点）。
#[tauri::command]
fn hud_front(app: AppHandle) {
    if let Some(win) = app.get_webview_window("hud") {
        let _ = win.show();
        let _ = win.set_always_on_top(true);
        let _ = win.eval("window.__hudWake && window.__hudWake(true)"); // 从菜单栏叫出来：重新计空闲时间，并强制重新摆位置
    }
}

/// 悬浮窗透明留白处让鼠标穿过去，点得到下面的窗口；网页按光标落点决定开关。
#[tauri::command]
fn hud_through(app: AppHandle, on: bool) {
    if let Some(win) = app.get_webview_window("hud") {
        let _ = win.set_ignore_cursor_events(on);
    }
}

/// macOS 上悬浮窗不是当前窗口时网页收不到鼠标移入（要先点一下才灵），
/// 所以由这里每 60 毫秒读一次全局光标，落在悬浮窗范围里就把相对坐标告诉网页，移出去报一次 null。
/// 系统级“鼠标按下过几次”（左键 + 右键）：只看计数变化，按得再快也不会漏。悬浮窗不是当前窗口时，点别的应用网页收不到事件，靠它判断“点了外面”
#[cfg(target_os = "macos")]
fn press_count() -> u64 {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceCounterForEventType(state: i32, event_type: u32) -> u32;
    }
    // kCGEventSourceStateCombinedSessionState = 0；kCGEventLeftMouseDown = 1，kCGEventRightMouseDown = 3
    unsafe { CGEventSourceCounterForEventType(0, 1) as u64 + CGEventSourceCounterForEventType(0, 3) as u64 }
}
#[cfg(target_os = "windows")]
fn press_count() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    #[link(name = "user32")]
    extern "system" {
        fn GetAsyncKeyState(key: i32) -> i16;
    }
    static N: AtomicU64 = AtomicU64::new(0);
    // 低位 = 上次查询以来按下过；VK_LBUTTON = 1，VK_RBUTTON = 2
    let hit = unsafe { (GetAsyncKeyState(1) & 1) != 0 || (GetAsyncKeyState(2) & 1) != 0 };
    if hit { N.fetch_add(1, Ordering::Relaxed) + 1 } else { N.load(Ordering::Relaxed) }
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn press_count() -> u64 {
    0
}

fn hover_loop(app: AppHandle) {
    let mut last: Option<(i32, i32)> = None;
    let mut presses = press_count();
    loop {
        std::thread::sleep(Duration::from_millis(60));
        let Some(win) = app.get_webview_window("hud") else { continue };
        if !win.is_visible().unwrap_or(false) {
            continue;
        }
        let (Ok(c), Ok(p), Ok(sz), Ok(k)) = (app.cursor_position(), win.outer_position(), win.outer_size(), win.scale_factor()) else { continue };
        let (x, y) = ((c.x - p.x as f64) / k, (c.y - p.y as f64) / k);
        let inside = x >= 0.0 && y >= 0.0 && x < sz.width as f64 / k && y < sz.height as f64 / k;
        let now = if inside { Some((x.round() as i32, y.round() as i32)) } else { None };
        let n = press_count();
        if n != presses {  // 按过鼠标：告诉网页按在哪（窗口外是 null），展开时点了面板以外就收起
            presses = n;
            let js = match now {
                Some((x, y)) => format!("window.__hudPress && window.__hudPress({x},{y})"),
                None => "window.__hudPress && window.__hudPress(null)".into(),
            };
            let _ = win.eval(&js);
        }
        if now == last {
            continue;
        }
        last = now;
        let js = match now {
            Some((x, y)) => format!("window.__hudHover && window.__hudHover({x},{y})"),
            None => "window.__hudHover && window.__hudHover(null)".into(),
        };
        let _ = win.eval(&js);
    }
}

#[tauri::command]
fn hud_visible(app: AppHandle, on: bool) {
    if let Some(win) = app.get_webview_window("hud") {
        let visible = win.is_visible().unwrap_or(false);
        if on && !visible {
            let _ = win.show();
        } else if !on && visible {
            let _ = win.hide();
        }
    }
}

/// 悬浮窗网页每 10 秒报一次平安：报不上来（没加载出来、网页进程崩了）就由后台重新加载它
#[tauri::command]
fn hud_beat() {
    HUD_BEAT.store(now_secs(), Ordering::Relaxed);
}

/// macOS：悬浮窗不跟着应用一起藏（⌘H、应用被收起、切走时都还在）
#[cfg(target_os = "macos")]
fn pin_hud(win: &tauri::WebviewWindow) {
    use std::ffi::{c_char, c_void};
    #[link(name = "objc")]
    extern "C" {
        fn objc_msgSend();
        fn sel_registerName(name: *const c_char) -> *const c_void;
    }
    let Ok(ns) = win.ns_window() else { return };
    unsafe {
        let send: extern "C" fn(*mut c_void, *const c_void, i8) = std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        send(ns, sel_registerName(c"setCanHide:".as_ptr()), 0);
        send(ns, sel_registerName(c"setHidesOnDeactivate:".as_ptr()), 0);
    }
}
#[cfg(not(target_os = "macos"))]
fn pin_hud(_win: &tauri::WebviewWindow) {}

#[tauri::command]
fn open_main(app: AppHandle, page: Option<String>) {
    show_main(&app, page.as_deref());
}

fn working_count(body: &str) -> usize {
    body.matches("\"state\": \"working\"").count()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![hud_resize, hud_front, hud_visible, open_main, hud_pos, hud_place, hud_snap, hud_through, hud_beat])
        .setup(|app| {
            // 只在菜单栏和悬浮窗里出现，不占 Dock
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let handle = app.handle().clone();
            applog(&format!("应用启动 {} {}", std::env::consts::ARCH, env!("CARGO_PKG_VERSION")));
            refresh_bundled_service(&handle);
            ensure_service(&handle);
            let hh = handle.clone();
            std::thread::spawn(move || hover_loop(hh));
            for _ in 0..25 {
                if http_get("/api/health").is_some() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(200));
            }

            // 主窗口不预先建（藏着也吃几百 MB），第一次打开时再建
            let url: tauri::Url = URL.parse().unwrap();

            WebviewWindowBuilder::new(app, "hud", WebviewUrl::External(url.join("hud.html").unwrap()))
                .title("agentbus 悬浮窗")
                .inner_size(260.0, 58.0)
                .decorations(false)
                .transparent(true)
                .shadow(false)
                .resizable(false)
                .always_on_top(true)
                .visible_on_all_workspaces(true)
                .skip_taskbar(true)
                .focused(false)
                .accept_first_mouse(true)
                .build()?;
            if let Some(w) = app.get_webview_window("hud") {
                pin_hud(&w);
            }
            let hud_built = Instant::now();

            let open = MenuItem::with_id(app, "open", "打开基地", true, None::<&str>)?;
            let hud = MenuItem::with_id(app, "hud", "显示悬浮窗", true, None::<&str>)?;
            let browser = MenuItem::with_id(app, "browser", "在浏览器里打开", true, None::<&str>)?;
            let reload = MenuItem::with_id(app, "reload", "刷新界面", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart", "重启本地服务", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出 agentbus", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open, &hud, &sep, &browser, &reload, &restart, &sep2, &quit])?;

            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
            let tray = TrayIconBuilder::with_id("main")
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("agentbus")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, e| match e.id.as_ref() {
                    "open" => show_main(app, None),
                    "hud" => hud_front(app.clone()),
                    "browser" => {
                        let opener = if cfg!(windows) { ("cmd", vec!["/c", "start", "", URL]) } else if cfg!(target_os = "macos") { ("open", vec![URL]) } else { ("xdg-open", vec![URL]) };
                        let mut c = Command::new(opener.0);
                        c.args(opener.1);
                        no_window(&mut c);
                        let _ = c.spawn();
                    }
                    "reload" => {
                        for label in ["main", "hud"] {
                            if let Some(w) = app.get_webview_window(label) {
                                let _ = w.eval("location.reload()");
                            }
                        }
                    }
                    "restart" => {
                        let _ = http_get("/api/shutdown"); // 让服务自己退出（各平台都一样）
                        std::thread::sleep(Duration::from_millis(800));
                        ensure_service(app);
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, e| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = e {
                        show_main(tray.app_handle(), None);
                        hud_front(tray.app_handle().clone()); // 顺便把悬浮窗叫出来（藏起来、缩成小球的都会醒）
                    }
                })
                .build(app)?;

            // 菜单栏图标旁显示几位在干活；服务挂了就拉起来，恢复后刷新两个窗口；悬浮窗报不上平安就重新加载它
            std::thread::spawn(move || {
                // down = 连续几次连不上端口（真挂了）；慢、超时不算挂
                let (mut down, mut was_down) = (0u32, false);
                let mut last_reload = Instant::now();
                loop {
                    match http_get("/api/state") {
                        Some(body) => {
                            down = 0;
                            if was_down {
                                applog("本地服务恢复，刷新窗口");
                                for label in ["main", "hud"] {
                                    if let Some(w) = handle.get_webview_window(label) {
                                        let _ = w.eval("location.reload()");
                                    }
                                }
                                was_down = false;
                                last_reload = Instant::now();
                            }
                            let n = working_count(&body);
                            let _ = tray.set_title(if n > 0 { Some(format!("{n}")) } else { None::<String> });
                            let _ = tray.set_tooltip(Some(if n > 0 { format!("agentbus · {n} 位在干活") } else { "agentbus · 都闲着".into() }));
                            // 悬浮窗开着却 45 秒没报平安（第一次加载时服务还没起来、网页进程崩了）：重新加载，最多 30 秒一次
                            if let Some(w) = handle.get_webview_window("hud") {
                                let beat = HUD_BEAT.load(Ordering::Relaxed);
                                let stale = if beat == 0 { hud_built.elapsed() > Duration::from_secs(20) } else { now_secs().saturating_sub(beat) > 45 };
                                if stale && w.is_visible().unwrap_or(false) && last_reload.elapsed() > Duration::from_secs(30) {
                                    applog(&format!("悬浮窗没报平安（上次 {beat}），重新加载"));
                                    let _ = w.navigate(URL.parse::<tauri::Url>().unwrap().join("hud.html").unwrap());
                                    last_reload = Instant::now();
                                }
                            }
                        }
                        None if service_listening() => {} // 连得上、只是这次没回应：服务在忙，不动它
                        None => {
                            down += 1;
                            if down >= 2 {
                                if !was_down {
                                    applog("本地服务连不上");
                                }
                                was_down = true;
                                ensure_service(&handle);
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_secs(4));
                }
            });
            Ok(())
        })
        .on_window_event(|w, e| match e {
            // 关主窗口：真的关掉（销毁网页进程，省内存），下次打开再建；悬浮窗不能被关掉（在设置里隐藏）
            WindowEvent::CloseRequested { api, .. } if w.label() != "main" => {
                let _ = w.hide();
                api.prevent_close();
            }
            // 点了别的地方：悬浮窗自己收起
            WindowEvent::Focused(false) if w.label() == "hud" => {
                if let Some(wv) = w.app_handle().get_webview_window("hud") {
                    let _ = wv.eval("window.__hudBlur && window.__hudBlur()");
                }
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("agentbus 启动失败")
        .run(|app, e| {
            // 点 Dock 图标重新打开主窗口：只有 macOS 有这个事件
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = e {
                show_main(app, None);
            }
            #[cfg(not(target_os = "macos"))]
            let _ = (app, e);
        });
}
