use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio::{net::TcpListener, process::Command};
use tokio_tungstenite::{accept_async, connect_async, tungstenite::Message};
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.iter().any(|a| a == "--peer") {
        let pos = args.iter().position(|a| a == "-port").unwrap();
        let (mut ws, _) = connect_async(format!("ws://127.0.0.1:{}", args[pos + 1])).await?;
        ws.send(Message::Text(
            json!({"event":"registerPlugin","uuid":"test"})
                .to_string()
                .into(),
        ))
        .await?;
        while let Some(Ok(msg)) = ws.next().await {
            if let Message::Text(raw) = msg {
                let e: Value = serde_json::from_str(&raw)?;
                if e["event"] == "willAppear" {
                    assert_eq!(e["payload"]["settings"]["api_key"], "mock-secret");
                    ws.send(Message::Text(json!({"event":"setTitle","context":e["context"],"payload":{"title":"42%"}}).to_string().into())).await?;
                } else if e["event"] == "keyUp" {
                    ws.send(Message::Text(
                        json!({"event":"mockClickReceived","context":e["context"]})
                            .to_string()
                            .into(),
                    ))
                    .await?;
                }
            }
        }
        return Ok(());
    }
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let mut bridge = Command::new("target/release/opendeck-style")
        .arg(std::env::current_exe()?)
        .arg("--peer")
        .arg("-port")
        .arg(listener.local_addr()?.port().to_string())
        .kill_on_drop(true)
        .spawn()?;
    let (socket, _) = listener.accept().await?;
    let mut host = accept_async(socket).await?;
    let registration = host.next().await.unwrap()?;
    assert!(registration.to_text()?.contains("registerPlugin"));
    for profile in ["Default", "apps"] {
        let context = format!("n4-example-device.{profile}.Keypad.3.0");
        let appear = json!({"event":"willAppear","action":"me.amankhanna.oasystem.cpu","context":context,"payload":{"settings":{"api_key":"mock-secret"}}});
        host.send(Message::Text(appear.to_string().into())).await?;
        let msg = host.next().await.unwrap()?;
        let value: Value = serde_json::from_str(msg.to_text()?)?;
        assert_eq!(
            value["event"],
            if profile == "Default" {
                "setImage"
            } else {
                "setTitle"
            }
        );
        host.send(Message::Text(
            json!({"event":"keyUp","context":context})
                .to_string()
                .into(),
        ))
        .await?;
        let msg = host.next().await.unwrap()?;
        let value: Value = serde_json::from_str(msg.to_text()?)?;
        assert_eq!(value["event"], "mockClickReceived");
    }
    host.close(None).await?;
    assert!(tokio::time::timeout(Duration::from_secs(3), bridge.wait())
        .await??
        .success());
    println!("Bridge smoke passed: registration/settings/click passthrough, Default-only rendering, orderly shutdown.");
    Ok(())
}
#[tokio::main]
async fn main() {
    tokio::time::timeout(Duration::from_secs(15), run())
        .await
        .unwrap()
        .unwrap()
}
