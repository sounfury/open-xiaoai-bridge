use crate::python::PythonManager;
use open_xiaoai::base::{AppError, VERSION};
use open_xiaoai::services::audio::config::AudioConfig;
use open_xiaoai::services::connect::data::{Event, Request, Response, Stream};
use open_xiaoai::services::connect::handler::MessageHandler;
use open_xiaoai::services::connect::message::{MessageManager, WsStream};
use open_xiaoai::services::connect::rpc::RPC;
use open_xiaoai::services::speaker::SpeakerManager;
use open_xiaoai::utils::task::TaskManager;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString, PyTuple};
use pyo3::Python;
use serde_json::{json, Value};
use std::env;
use std::sync::{LazyLock, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
use tokio::time::timeout;
use tokio_tungstenite::{accept_async, accept_hdr_async};

pub struct AppServer;

/// 握手超过这个时间未完成就放弃该连接。
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// 启动后首次连接、或音箱离线超过这个时长后重连，才播报"已连接"；
/// Wi-Fi 抖动导致的快速重连不重复播报。
const ANNOUNCE_AFTER: Duration = Duration::from_secs(60);

/// 当前正在服务的音箱连接（一个 bridge 只服务一台音箱）。
static ACTIVE: LazyLock<Mutex<Option<JoinHandle<()>>>> = LazyLock::new(|| Mutex::new(None));
static LAST_DISCONNECT: LazyLock<StdMutex<Option<Instant>>> =
    LazyLock::new(|| StdMutex::new(None));
/// 设置后，每次 start_recording 都带上 `echo_ref`：音箱改发 [麦克风, 播放回采] 双声道。
static ECHO_REF: LazyLock<StdMutex<Option<Value>>> = LazyLock::new(|| StdMutex::new(None));

pub fn set_echo_ref(config: Option<Value>) {
    *ECHO_REF.lock().unwrap() = config;
}

/// start_recording 的请求参数：16 kHz 单声道 S16；设置了 echo_ref 时附带该字段。
/// 不认识 `echo_ref` 的客户端会忽略它，照旧录单声道。
pub fn recording_config() -> Value {
    let mut config = json!(AudioConfig {
        pcm: "noop".into(),
        channels: 1,
        bits_per_sample: 16,
        sample_rate: 16000,
        period_size: 1440 / 4,
        buffer_size: 1440,
    });
    if let Some(echo_ref) = ECHO_REF.lock().unwrap().clone() {
        config["echo_ref"] = echo_ref;
    }
    config
}

/// Check if audio input is enabled via environment variable.
/// Supports: "true"/"false", "1"/"0", "yes"/"no", etc.
/// Defaults to true if not set or invalid.
fn is_audio_input_enabled() -> bool {
    match env::var("AUDIO_INPUT_ENABLE") {
        Ok(val) => {
            let val = val.trim().to_lowercase();
            matches!(val.as_str(), "true" | "1" | "yes" | "on")
        }
        Err(_) => true,
    }
}

async fn on_connected(announce: bool) -> Result<(), AppError> {
    // 先开麦，欢迎语放后台，不能拖慢录音启动
    if is_audio_input_enabled() {
        let _ = RPC::instance()
            .call_remote("start_recording", Some(recording_config()), Some(3_000))
            .await;
    }

    if announce {
        SpeakerManager::play_text("已连接").await?;
    }

    // aplay is started lazily by ensure_player_ready() on first audio send,
    // avoiding empty-buffer underruns from idling aplay processes.

    Ok(())
}

impl AppServer {
    pub async fn connect(stream: TcpStream) -> Result<WsStream, AppError> {
        let expected_token = std::env::var("OPEN_XIAOAI_TOKEN").unwrap_or_default();
        if !expected_token.is_empty() {
            use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
            let ws_stream = accept_hdr_async(stream, move |req: &Request, response: Response| {
                let auth = req
                    .headers()
                    .get("Authorization")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("");
                if auth != format!("Bearer {}", expected_token) {
                    let error: ErrorResponse = tokio_tungstenite::tungstenite::http::Response::builder()
                        .status(401)
                        .body(Some("Unauthorized".to_string()))
                        .unwrap();
                    return Err(error);
                }
                Ok(response)
            })
            .await?;
            Ok(WsStream::Server(ws_stream))
        } else {
            let ws_stream = accept_async(stream).await?;
            Ok(WsStream::Server(ws_stream))
        }
    }

    pub async fn run() {
        let addr = "0.0.0.0:4399";
        let listener = TcpListener::bind(&addr)
            .await
            .expect(format!("[AppServer] ❌ 绑定地址失败: {}", &addr).as_str());
        crate::pylog!("[AppServer] ✅ 已启动: {:?}", addr);
        while let Ok((stream, addr)) = listener.accept().await {
            tokio::spawn(AppServer::take_over(stream, addr));
        }
    }

    /// 同一时刻只服务一个连接。音箱发起新连接，说明它已经放弃旧连接（或重启了），
    /// 立即关掉旧连接，而不是让音箱等到这边超时。
    async fn take_over(stream: TcpStream, addr: std::net::SocketAddr) {
        let ws_stream = match timeout(HANDSHAKE_TIMEOUT, AppServer::connect(stream)).await {
            Ok(Ok(ws_stream)) => ws_stream,
            Ok(Err(e)) => {
                let msg = e.to_string();
                if msg.contains("401") || msg.contains("Unauthorized") {
                    crate::pylog!("[AppServer] ❌ 鉴权失败: {}", addr);
                } else {
                    crate::pylog!("[AppServer] ❌ 连接异常: {} ({})", addr, msg);
                }
                return;
            }
            Err(_) => {
                crate::pylog!("[AppServer] ❌ 握手超时: {}", addr);
                return;
            }
        };
        let mut active = ACTIVE.lock().await;
        if let Some(old) = active.take() {
            if !old.is_finished() {
                MessageManager::instance().cancel("replaced by a new connection");
                let _ = old.await;
            }
        }
        *active = Some(tokio::spawn(AppServer::handle_connection(ws_stream, addr)));
    }

    async fn handle_connection(ws_stream: WsStream, addr: std::net::SocketAddr) {
        crate::pylog!("[AppServer] ✅ 已连接: {:?}", addr);
        AppServer::init(ws_stream).await;
        let reason = match MessageManager::instance().process_messages().await {
            Ok(()) => "closed by speaker".to_string(),
            Err(e) => e.to_string(),
        };
        AppServer::dispose().await;
        *LAST_DISCONNECT.lock().unwrap() = Some(Instant::now());
        crate::pylog!("[AppServer] ❌ 已断开连接: {}", reason);
    }

    async fn init(ws_stream: WsStream) {
        MessageManager::instance().init(ws_stream).await;
        MessageHandler::<Event>::instance()
            .set_handler(on_event)
            .await;
        MessageHandler::<Stream>::instance()
            .set_handler(on_stream)
            .await;

        let rpc = RPC::instance();
        rpc.add_command("get_version", get_version).await;

        let announce = LAST_DISCONNECT
            .lock()
            .unwrap()
            .map_or(true, |at| at.elapsed() >= ANNOUNCE_AFTER);
        let task = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(1)).await;
            let _ = on_connected(announce).await;
        });
        TaskManager::instance().add("on_connected", task).await;
    }

    async fn dispose() {
        MessageManager::instance().dispose().await;
        TaskManager::instance().dispose("on_connected").await;
    }
}

async fn get_version(_: Request) -> Result<Response, AppError> {
    let data = json!(VERSION.to_string());
    Ok(Response::from_data(data))
}

async fn on_stream(stream: Stream) -> Result<(), AppError> {
    let Stream {
        tag,
        bytes,
        data: metadata,
        ..
    } = stream;
    match tag.as_str() {
        "record" => {
            // 新客户端的录音帧带 metadata（seq、capture_id，双声道时 layout=mic_ref）。
            // Python 侧注册了 on_input_packet 就交给它，否则退回 on_input_data。
            let manager = PythonManager::instance();
            if let Some(metadata) = metadata.filter(|_| manager.has_fn("on_input_packet")) {
                let metadata = serde_json::to_string(&metadata)?;
                let packet = Python::with_gil(|py| -> PyResult<PyObject> {
                    Ok(PyTuple::new(
                        py,
                        [
                            PyBytes::new(py, &bytes).into_any(),
                            PyString::new(py, &metadata).into_any(),
                        ],
                    )?
                    .into())
                })?;
                manager.call_fn("on_input_packet", Some(packet))?;
                return Ok(());
            }
            let data = Python::with_gil(|py| PyBytes::new(py, &bytes).into());
            manager.call_fn("on_input_data", Some(data))?;
        }
        _ => {}
    }
    Ok(())
}

async fn on_event(event: Event) -> Result<(), AppError> {
    let event_json = serde_json::to_string(&event)?;
    let data = Python::with_gil(|py| PyString::new(py, &event_json).into());
    PythonManager::instance().call_fn("on_event", Some(data))?;
    Ok(())
}
