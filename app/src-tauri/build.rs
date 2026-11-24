fn main() {
    // 悬浮窗和主窗口的页面来自本地服务（远程地址），应用自己的命令要显式放行
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["hud_resize", "hud_front", "hud_visible", "open_main", "hud_pos", "hud_place", "hud_snap", "hud_through", "hud_beat"]),
    ))
    .expect("tauri build 失败");
}
