//! Local presentation-only bridge. Original plugins retain their actions, API calls,
//! credentials and settings. Only Default-page display messages are transformed.
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use opendeck_style::*;
use serde_json::{json, Value};
use std::{collections::HashMap, time::Duration};
use tokio::{net::TcpListener, process::Command};
use tokio_tungstenite::{accept_async, connect_async, tungstenite::Message};

fn on_default(context: &str) -> bool {
    let parts: Vec<_> = context.split('.').collect();
    parts.len() == 5 && parts[0].starts_with("n4-") && parts[1] == "Default" && parts[2] == "Keypad"
}
fn image_event(context: &str, svg: String) -> Value {
    json!({"event":"setImage","context":context,"payload":{"image":uri(&svg),"target":0}})
}
fn artwork_action(action: &str) -> Option<&str> {
    match action {
        "me.amankhanna.oampris.previous" => Some("previous"),
        "me.amankhanna.oampris.next" => Some("next"),
        "me.amankhanna.oampris.playpause" => Some("playpause"),
        _ => None,
    }
}
// Parse only plain text nodes from known SVG image output; never log raw traffic.
fn credit_text(image: &str) -> Option<String> {
    let data = image.strip_prefix("data:image/svg+xml;base64,")?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data)
        .ok()?;
    let svg = std::str::from_utf8(&bytes).ok()?;
    for node in svg.split("<text").skip(1) {
        let value = node.split_once('>')?.1.split_once("</text>")?.0.trim();
        if value.starts_with('$')
            && value.len() < 24
            && value[1..]
                .chars()
                .all(|c| c.is_ascii_digit() || c == '.' || c == ',' || c == '-')
        {
            return Some(value.to_owned());
        }
    }
    None
}
fn transform(mut event: Value, actions: &HashMap<String, String>) -> Value {
    let Some(context) = event["context"].as_str().map(str::to_owned) else {
        return event;
    };
    if !on_default(&context) {
        return event;
    }
    let Some(action) = actions.get(&context) else {
        return event;
    };
    match event["event"].as_str() {
        Some("setTitle") if action.starts_with("me.amankhanna.oasystem.") => {
            let title = event["payload"]["title"].as_str().unwrap_or("--");
            let svg = if action.ends_with(".cpu") {
                metric(
                    "CPU",
                    title,
                    "PROCESSOR",
                    BLUE,
                    title.trim_end_matches('%').parse().ok(),
                )
            } else if action.ends_with(".ram") {
                metric("MEMORY", title, "RAM IN USE", LILAC, None)
            } else if action.ends_with(".uptime") {
                let parts: Vec<_> = title.lines().collect();
                card(
                    "UPTIME",
                    MINT,
                    &format!(
                        "{}{}{}",
                        text(
                            72,
                            70,
                            23,
                            "#eef4ff",
                            "middle",
                            parts.first().copied().unwrap_or("--")
                        ),
                        text(
                            72,
                            99,
                            18,
                            "#a8b6cb",
                            "middle",
                            parts.get(1).copied().unwrap_or("")
                        ),
                        text(72, 126, 11, MINT, "middle", "SYSTEM ONLINE")
                    ),
                )
            } else {
                return event;
            };
            event["event"] = json!("setImage");
            event["payload"].as_object_mut().unwrap().remove("title");
            event["payload"]["image"] = json!(uri(&svg));
        }
        Some("setImage") if action == "com.imdevinc.openroutercredits.credits" => {
            let image = event["payload"]["image"].as_str().unwrap_or("");
            let Some(credits) = credit_text(image) else {
                return event;
            }; // Preserve original error/unknown states.
            event["payload"]["image"] = json!(uri(&metric(
                "OPENROUTER",
                &credits,
                "CREDITS LEFT",
                LILAC,
                None
            )));
        }
        Some("setImage") => {
            if let Some(kind) = artwork_action(action) {
                let svg = control(kind, event["payload"]["image"].as_str());
                event["payload"]["image"] = json!(uri(&svg));
            }
        }
        _ => {}
    }
    event
}
async fn bridge() -> Result<(), Box<dyn std::error::Error>> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 2 {
        return Err("Missing plugin path".into());
    }
    let original = std::path::PathBuf::from(args.remove(0));
    if !original.is_absolute() {
        return Err("Plugin path must be absolute".into());
    }
    let port_pos = args
        .iter()
        .position(|a| a == "-port")
        .ok_or("Missing port")?
        + 1;
    let host_port: u16 = args.get(port_pos).ok_or("Missing port")?.parse()?;
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    args[port_pos] = listener.local_addr()?.port().to_string();
    let (mut host, _) = connect_async(format!("ws://127.0.0.1:{host_port}")).await?;
    let mut child = Command::new(&original)
        .args(args)
        .kill_on_drop(true)
        .spawn()?;
    let (socket, _) = tokio::time::timeout(Duration::from_secs(20), listener.accept()).await??;
    let mut plugin = tokio::time::timeout(Duration::from_secs(10), accept_async(socket)).await??;
    drop(listener);
    let mut actions = HashMap::new();
    loop {
        tokio::select! {
            msg=host.next()=>{
                let Some(Ok(msg))=msg else{break};
                if let Message::Text(raw)=&msg {
                    if let Ok(event)=serde_json::from_str::<Value>(raw) {
                        let context=event["context"].as_str().unwrap_or("");
                        if event["event"]=="willAppear" {
                            if let Some(action)=event["action"].as_str() {
                                actions.insert(context.to_owned(),action.to_owned());
                                if on_default(context) {
                                    if let Some(kind)=artwork_action(action){host.send(Message::Text(image_event(context,control(kind,None)).to_string().into())).await?;}
                                }
                            }
                        }else if event["event"]=="willDisappear" {actions.remove(context);}
                    }
                }
                let close=matches!(msg,Message::Close(_));
                plugin.send(msg).await?;
                if close{break}
            },
            msg=plugin.next()=>{
                let Some(Ok(mut msg))=msg else{break};
                if let Message::Text(raw)=&msg {
                    if let Ok(event)=serde_json::from_str::<Value>(raw){msg=Message::Text(transform(event,&actions).to_string().into());}
                }
                let close=matches!(msg,Message::Close(_));
                host.send(msg).await?;
                if close{break}
            },
            _=child.wait()=>{break}
        }
    }
    let _ = child.kill().await;
    let _ = child.wait().await;
    Ok(())
}
#[tokio::main]
async fn main() {
    if bridge().await.is_err() {
        eprintln!("Deck style bridge stopped; no message contents logged");
        std::process::exit(1)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn mapping(context: &str, action: &str) -> HashMap<String, String> {
        HashMap::from([(context.into(), action.into())])
    }
    #[test]
    fn default_only_and_passthrough() {
        let c = "n4-example-device.apps.Keypad.3.0";
        let e = json!({"event":"setTitle","context":c,"payload":{"title":"4%"}});
        assert_eq!(
            transform(e.clone(), &mapping(c, "me.amankhanna.oasystem.cpu")),
            e
        );
        let e = json!({"event":"setSettings","context":"n4-example-device.Default.Keypad.0.0","payload":{"api_key":"test-secret"}});
        assert_eq!(transform(e.clone(), &HashMap::new()), e);
    }
    #[test]
    fn supports_another_n4_device_without_affecting_other_profiles() {
        assert!(on_default("n4-friends-device.Default.Keypad.0.0"));
        assert!(!on_default("n4-friends-device.apps.Keypad.0.0"));
        assert!(!on_default("other-device.Default.Keypad.0.0"));
    }
    #[test]
    fn system_updates() {
        let c = "n4-example-device.Default.Keypad.3.0";
        let e = transform(
            json!({"event":"setTitle","context":c,"payload":{"title":"42%","state":0,"target":0}}),
            &mapping(c, "me.amankhanna.oasystem.cpu"),
        );
        assert_eq!(e["event"], "setImage");
        assert_eq!(e["payload"]["state"], 0);
        assert!(e["payload"].get("title").is_none());
    }
    #[test]
    fn openrouter_success_and_error() {
        assert_eq!(
            credit_text(&uri("<svg><text>CREDITS</text><text>$14.40</text></svg>")),
            Some("$14.40".into())
        );
        assert!(credit_text(&uri("<svg><text>Unavailable</text></svg>")).is_none());
    }
    #[test]
    fn art_is_retained() {
        let svg = control("playpause", Some("data:image/jpeg;base64,YQ=="));
        assert!(svg.contains("YQ=="));
        assert!(svg.contains("PLAY / PAUSE"));
    }
}
