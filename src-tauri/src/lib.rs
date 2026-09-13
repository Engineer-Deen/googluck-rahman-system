#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let sidecar_command = tauri_plugin_shell::ShellExt::shell(app.handle())
                .sidecar("goodluck-backend")
                .expect("failed to create backend sidecar command");

            tauri::async_runtime::spawn(async move {
                match sidecar_command.spawn() {
                    Ok((_rx, _child)) => {
                        println!("Good Luck Rahman backend started successfully.");
                    }
                    Err(error) => {
                        eprintln!("Failed to start Good Luck Rahman backend: {error}");
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}