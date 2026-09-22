use std::sync::Mutex;

use serde::Serialize;
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_updater::UpdaterExt;

struct BackendSidecar {
    child: Mutex<Option<CommandChild>>,
    spawn_error: Mutex<Option<String>>,
    #[cfg(windows)]
    job: Mutex<Option<windows_job::JobHandle>>,
}

mod windows_job {
    #![cfg(windows)]

    use std::ffi::c_void;
    use std::ptr;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateJobObjectW(attrs: *mut c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(
            job: *mut c_void,
            info_class: u32,
            info: *mut c_void,
            info_len: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }

    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    const JobObjectExtendedLimitInformation: u32 = 9;
    const PROCESS_ALL_ACCESS: u32 = 0x001F_FFFF;

    #[repr(C)]
    struct IoCounters {
        read_op: u64,
        write_op: u64,
        other_op: u64,
        read_xfer: u64,
        write_xfer: u64,
        other_xfer: u64,
    }

    #[repr(C)]
    struct JobObjectBasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct JobObjectExtendedLimitInformation {
        basic: JobObjectBasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    pub struct JobHandle(*mut c_void);

    unsafe impl Send for JobHandle {}

    impl Drop for JobHandle {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    CloseHandle(self.0);
                }
            }
        }
    }

    pub fn create_kill_on_close_job() -> Option<JobHandle> {
        unsafe {
            let job = CreateJobObjectW(ptr::null_mut(), ptr::null());
            if job.is_null() {
                return None;
            }
            let mut info = JobObjectExtendedLimitInformation {
                basic: JobObjectBasicLimitInformation {
                    per_process_user_time_limit: 0,
                    per_job_user_time_limit: 0,
                    limit_flags: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                    minimum_working_set_size: 0,
                    maximum_working_set_size: 0,
                    active_process_limit: 0,
                    affinity: 0,
                    priority_class: 0,
                    scheduling_class: 0,
                },
                io_info: IoCounters {
                    read_op: 0,
                    write_op: 0,
                    other_op: 0,
                    read_xfer: 0,
                    write_xfer: 0,
                    other_xfer: 0,
                },
                process_memory_limit: 0,
                job_memory_limit: 0,
                peak_process_memory_used: 0,
                peak_job_memory_used: 0,
            };
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &mut info as *mut _ as *mut c_void,
                std::mem::size_of::<JobObjectExtendedLimitInformation>() as u32,
            );
            if ok == 0 {
                CloseHandle(job);
                return None;
            }
            Some(JobHandle(job))
        }
    }

    pub fn assign_pid(job: &JobHandle, pid: u32) -> bool {
        unsafe {
            let process = OpenProcess(PROCESS_ALL_ACCESS, 0, pid);
            if process.is_null() {
                return false;
            }
            let ok = AssignProcessToJobObject(job.0, process);
            CloseHandle(process);
            ok != 0
        }
    }

    pub fn assign_process_tree(job: &JobHandle, root_pid: u32) {
        let _ = assign_pid(job, root_pid);
        // Best-effort: include already-spawned children (PyInstaller one-file).
        unsafe {
            #[repr(C)]
            struct ProcessEntry32W {
                dw_size: u32,
                cnt_usage: u32,
                th32_process_id: u32,
                th32_default_heap_id: usize,
                th32_module_id: u32,
                cnt_threads: u32,
                th32_parent_process_id: u32,
                pc_pri_class_base: i32,
                dw_flags: u32,
                sz_exe_file: [u16; 260],
            }
            #[link(name = "kernel32")]
            unsafe extern "system" {
                fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> *mut c_void;
                fn Process32FirstW(snapshot: *mut c_void, entry: *mut ProcessEntry32W) -> i32;
                fn Process32NextW(snapshot: *mut c_void, entry: *mut ProcessEntry32W) -> i32;
            }
            const TH32CS_SNAPPROCESS: u32 = 0x00000002;
            const INVALID: isize = -1;
            let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if snap as isize == INVALID {
                return;
            }
            let mut entry = ProcessEntry32W {
                dw_size: std::mem::size_of::<ProcessEntry32W>() as u32,
                cnt_usage: 0,
                th32_process_id: 0,
                th32_default_heap_id: 0,
                th32_module_id: 0,
                cnt_threads: 0,
                th32_parent_process_id: 0,
                pc_pri_class_base: 0,
                dw_flags: 0,
                sz_exe_file: [0; 260],
            };
            let mut pids = vec![root_pid];
            if Process32FirstW(snap, &mut entry) != 0 {
                loop {
                    if pids.contains(&entry.th32_parent_process_id)
                        && !pids.contains(&entry.th32_process_id)
                    {
                        let _ = assign_pid(job, entry.th32_process_id);
                        pids.push(entry.th32_process_id);
                    }
                    if Process32NextW(snap, &mut entry) == 0 {
                        break;
                    }
                }
            }
            CloseHandle(snap);
        }
    }
}

fn stop_backend_sidecar(app: &tauri::AppHandle) {
    let Some(state) = app.try_state::<BackendSidecar>() else {
        return;
    };

    let child = state.child.lock().ok().and_then(|mut guard| guard.take());
    if let Some(child) = child {
        let pid = child.pid();
        let _ = child.kill();
        #[cfg(windows)]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status();
        }
    }

    #[cfg(windows)]
    {
        // Dropping the job handle kills remaining assigned processes.
        let _job = state.job.lock().ok().and_then(|mut guard| guard.take());
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct LocalBackendStatus {
    spawned: bool,
    spawn_error: Option<String>,
}

/// Report whether the local POS sidecar was spawned, or why spawn failed.
#[tauri::command]
fn glr_local_backend_status(app: AppHandle) -> LocalBackendStatus {
    let state = app.state::<BackendSidecar>();
    let spawned = state
        .child
        .lock()
        .map(|guard| guard.is_some())
        .unwrap_or(false);
    let spawn_error = state
        .spawn_error
        .lock()
        .ok()
        .and_then(|guard| guard.clone());
    LocalBackendStatus {
        spawned,
        spawn_error,
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateAvailableInfo {
    version: String,
    current_version: String,
    body: String,
}

/// Background-safe update check. Network/config failures return Ok(None)
/// so POS operation is never interrupted by updater issues.
#[tauri::command]
async fn glr_check_update(app: AppHandle) -> Result<Option<UpdateAvailableInfo>, String> {
    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(error) => {
            log::info!("updater unavailable: {error}");
            return Ok(None);
        }
    };

    match updater.check().await {
        Ok(Some(update)) => Ok(Some(UpdateAvailableInfo {
            version: update.version.clone(),
            current_version: update.current_version.clone(),
            body: update.body.clone().unwrap_or_default(),
        })),
        Ok(None) => Ok(None),
        Err(error) => {
            log::info!("update check failed (non-fatal): {error}");
            Ok(None)
        }
    }
}

/// Download, verify, install the signed update, then relaunch.
#[tauri::command]
async fn glr_install_update(app: AppHandle) -> Result<(), String> {
    let updater = app.updater().map_err(|error| error.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "No update is currently available.".to_string())?;

    update
        .download_and_install(|_chunk_len, _content_len| {}, || {})
        .await
        .map_err(|error| error.to_string())?;

    // Stop sidecar cleanly before process restart replaces binaries.
    stop_backend_sidecar(&app);
    app.restart();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(BackendSidecar {
            child: Mutex::new(None),
            spawn_error: Mutex::new(None),
            #[cfg(windows)]
            job: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            glr_check_update,
            glr_install_update,
            glr_local_backend_status
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Shop PCs must always run the local POS sidecar. Explicit env
            // overrides prevent a developer shell (GLR_MODE=central / PORT=8000)
            // from poisoning the desktop child process.
            let sidecar_command = tauri_plugin_shell::ShellExt::shell(app.handle())
                .sidecar("goodluck-backend")
                .expect("failed to create backend sidecar command")
                .env("GLR_MODE", "local")
                .env("PORT", "5000")
                .env("CENTRAL_SYNC_URL", "https://goodluck-rahman-api.vercel.app");

            match sidecar_command.spawn() {
                Ok((_rx, child)) => {
                    #[cfg(windows)]
                    {
                        if let Some(job) = windows_job::create_kill_on_close_job() {
                            let pid = child.pid();
                            // Brief pause so PyInstaller can spawn its worker child.
                            std::thread::sleep(std::time::Duration::from_millis(400));
                            windows_job::assign_process_tree(&job, pid);
                            *app.state::<BackendSidecar>().job.lock().expect("job lock") = Some(job);
                        }
                    }
                    *app.state::<BackendSidecar>()
                        .child
                        .lock()
                        .expect("sidecar lock") = Some(child);
                    *app.state::<BackendSidecar>()
                        .spawn_error
                        .lock()
                        .expect("spawn error lock") = None;
                    println!("Good Luck Rahman backend started successfully.");
                }
                Err(error) => {
                    let message = format!(
                        "Local POS server failed to start ({error}). Reinstall Good Luck Rahman or contact support."
                    );
                    eprintln!("{message}");
                    *app.state::<BackendSidecar>()
                        .spawn_error
                        .lock()
                        .expect("spawn error lock") = Some(message);
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(
                event,
                WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed
            ) {
                stop_backend_sidecar(window.app_handle());
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                stop_backend_sidecar(app_handle);
            }
        });
}

#[cfg(test)]
mod local_backend_status_tests {
    use super::LocalBackendStatus;

    #[test]
    fn serializes_spawn_error_for_frontend() {
        let status = LocalBackendStatus {
            spawned: false,
            spawn_error: Some(
                "Local POS server failed to start (missing binary). Reinstall Good Luck Rahman or contact support."
                    .to_string(),
            ),
        };
        let json = serde_json::to_value(&status).expect("serialize");
        assert_eq!(json["spawned"], false);
        assert_eq!(
            json["spawnError"],
            "Local POS server failed to start (missing binary). Reinstall Good Luck Rahman or contact support."
        );
    }

    #[test]
    fn serializes_successful_spawn_without_error() {
        let status = LocalBackendStatus {
            spawned: true,
            spawn_error: None,
        };
        let json = serde_json::to_value(&status).expect("serialize");
        assert_eq!(json["spawned"], true);
        assert!(json.get("spawnError").unwrap().is_null());
    }
}
