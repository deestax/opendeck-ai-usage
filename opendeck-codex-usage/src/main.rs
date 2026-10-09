use base64::Engine;
use openaction::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

const REFRESH: Duration = Duration::from_secs(300);
const TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone, Default, Serialize, Deserialize)]
struct Settings {}

#[derive(Clone, Default)]
struct State {
    last_fetch: Option<Instant>,
    usage: Option<Usage>,
    error: bool,
}
static INSTANCES: LazyLock<Mutex<HashMap<String, State>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

// Only the public, documented read-only account/rateLimits/read RPC is used.
// The Codex process uses its existing sign-in; this plugin never reads auth.json.
async fn rate_limits() -> Result<Value, String> {
    let mut child = Command::new("codex")
        .arg("app-server")
        .arg("--stdio")
        .kill_on_drop(true)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("Codex unavailable: {e}"))?;
    let mut stdin = child.stdin.take().ok_or("Missing Codex stdin")?;
    let stdout = child.stdout.take().ok_or("Missing Codex stdout")?;
    let mut lines = BufReader::new(stdout).lines();

    let result = tokio::time::timeout(TIMEOUT, async {
		send(&mut stdin, json!({"id": 1, "method": "initialize", "params": {
			"clientInfo": {"name": "opendeck-codex-usage", "title": "OpenDeck Codex Usage", "version": "0.1.0"},
			"capabilities": null
		}})).await?;
		let init = receive(&mut lines, 1).await?;
		if let Some(error) = init.get("error") {
			return Err(format!("Codex init: {}", error["message"].as_str().unwrap_or("failed")));
		}
		send(&mut stdin, json!({"method": "initialized", "params": {}})).await?;
		send(&mut stdin, json!({"id": 2, "method": "account/rateLimits/read"})).await?;
		let response = receive(&mut lines, 2).await?;
		if let Some(error) = response.get("error") {
			return Err(format!("Codex limits: {}", error["message"].as_str().unwrap_or("failed")));
		}
		response.get("result").cloned().ok_or_else(|| "No rate limits returned".into())
	}).await;
    // Explicitly stop the child; no background Codex process or token cache is retained.
    let _ = child.kill().await;
    match result {
        Ok(inner) => inner,
        Err(_) => Err("Codex timed out".into()),
    }
}

async fn send(stdin: &mut tokio::process::ChildStdin, value: Value) -> Result<(), String> {
    stdin
        .write_all(value.to_string().as_bytes())
        .await
        .map_err(|e| e.to_string())?;
    stdin.write_all(b"\n").await.map_err(|e| e.to_string())?;
    stdin.flush().await.map_err(|e| e.to_string())
}

async fn receive(
    lines: &mut tokio::io::Lines<BufReader<tokio::process::ChildStdout>>,
    id: i64,
) -> Result<Value, String> {
    loop {
        let line = lines
            .next_line()
            .await
            .map_err(|e| e.to_string())?
            .ok_or("Codex closed connection")?;
        let value: Value = serde_json::from_str(&line).map_err(|_| "Invalid Codex response")?;
        if value["id"].as_i64() == Some(id) {
            return Ok(value);
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
struct Usage {
    five_hour: Option<u8>,
    weekly: Option<u8>,
    five_reset: Option<i64>,
    week_reset: Option<i64>,
}

fn parse_usage(result: &Value) -> Result<Usage, String> {
    let limits = result
        .pointer("/rateLimitsByLimitId/codex")
        .or_else(|| result.get("rateLimits"))
        .ok_or("No Codex quota returned")?;
    let mut usage = Usage::default();
    for key in ["primary", "secondary"] {
        let Some(window) = limits.get(key) else {
            continue;
        };
        let (Some(minutes), Some(percent)) = (
            window["windowDurationMins"].as_u64(),
            window["usedPercent"].as_f64(),
        ) else {
            continue;
        };
        if !percent.is_finite() {
            continue;
        }
        let used = percent.round().clamp(0.0, 100.0) as u8;
        let reset = window["resetsAt"].as_i64().filter(|at| *at > 0);
        if minutes == 7 * 24 * 60 {
            usage.weekly = Some(used);
            usage.week_reset = reset;
        } else if minutes == 5 * 60 {
            usage.five_hour = Some(used);
            usage.five_reset = reset;
        }
    }
    if usage.five_hour.is_none() && usage.weekly.is_none() {
        return Err("Codex quota windows unavailable".into());
    }
    Ok(usage)
}

fn image(usage: &Usage, stale: bool) -> String {
    opendeck_style::quota(
        "CODEX",
        opendeck_style::MINT,
        usage.five_hour.map(f64::from),
        usage.weekly.map(f64::from),
        usage.five_reset,
        usage.week_reset,
        stale,
        opendeck_style::now(),
    )
}

async fn display(instance: &Instance, svg: &str) {
    let uri = format!(
        "data:image/svg+xml;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(svg)
    );
    if let Err(e) = instance.set_image(Some(uri), None).await {
        log::warn!("Could not update Codex key: {e}");
    }
}

async fn refresh(instance: &Instance, force: bool) {
    let id = instance.instance_id.clone();
    let due = {
        let mut states = INSTANCES.lock().unwrap();
        let Some(state) = states.get_mut(&id) else {
            return;
        };
        let due = state.last_fetch.is_none_or(|at| {
            at.elapsed() >= REFRESH
                || (force && !state.error && at.elapsed() >= Duration::from_secs(30))
        });
        if due {
            state.last_fetch = Some(Instant::now());
        }
        due
    };
    if due {
        let result = rate_limits().await.and_then(|result| parse_usage(&result));
        let mut states = INSTANCES.lock().unwrap();
        if let Some(state) = states.get_mut(&id) {
            match result {
                Ok(usage) => {
                    state.usage = Some(usage);
                    state.error = false;
                }
                Err(_) => {
                    state.error = true;
                    log::warn!("Codex usage unavailable; retaining last reading");
                }
            }
        }
    }
    // Redraw relative time from cached absolute reset timestamps, without extra RPCs.
    let svg = {
        let states = INSTANCES.lock().unwrap();
        let Some(state) = states.get(&id) else { return };
        state
            .usage
            .as_ref()
            .map(|u| image(u, state.error))
            .unwrap_or_else(|| {
                opendeck_style::metric("CODEX", "--", "unavailable", opendeck_style::MINT, None)
            })
    };
    display(instance, &svg).await;
}

struct UsageAction;
#[async_trait]
impl Action for UsageAction {
    const UUID: ActionUuid = "local.superdao.codexusage.usage";
    type Settings = Settings;
    async fn will_appear(&self, instance: &Instance, _: &Settings) -> OpenActionResult<()> {
        INSTANCES
            .lock()
            .unwrap()
            .entry(instance.instance_id.clone())
            .or_default();
        refresh(instance, false).await;
        Ok(())
    }
    async fn will_disappear(&self, instance: &Instance, _: &Settings) -> OpenActionResult<()> {
        INSTANCES.lock().unwrap().remove(&instance.instance_id);
        Ok(())
    }
    async fn key_up(&self, instance: &Instance, _: &Settings) -> OpenActionResult<()> {
        refresh(instance, true).await;
        Ok(())
    }
}

#[tokio::main]
async fn main() -> OpenActionResult<()> {
    if std::env::args().nth(1).as_deref() == Some("--check") {
        match rate_limits().await {
            Ok(data) => println!("{}", data),
            Err(_) => {
                eprintln!("Codex usage unavailable");
                std::process::exit(1);
            }
        }
        return Ok(());
    }
    use simplelog::*;
    let _ = TermLogger::init(
        LevelFilter::Info,
        Config::default(),
        TerminalMode::Stdout,
        ColorChoice::Never,
    );
    register_action(UsageAction).await;
    let ticker = tokio::spawn(async {
        let mut interval = tokio::time::interval(Duration::from_secs(30));
        loop {
            interval.tick().await;
            for instance in visible_instances(UsageAction::UUID).await {
                refresh(&instance, false).await;
            }
        }
    });
    let result = run(std::env::args().collect()).await;
    ticker.abort();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn handles_missing_and_reordered_windows() {
        let weekly = json!({"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":70,"windowDurationMins":10080},"secondary":null}}});
        assert_eq!(
            parse_usage(&weekly).unwrap(),
            Usage {
                five_hour: None,
                weekly: Some(70),
                ..Usage::default()
            }
        );
        assert!(image(&parse_usage(&weekly).unwrap(), false).contains("reset --"));
        let both = json!({"rateLimits":{"primary":{"usedPercent":31.5,"windowDurationMins":10080,"resetsAt":1791028800},"secondary":{"usedPercent":12,"windowDurationMins":300}}});
        assert_eq!(
            parse_usage(&both).unwrap(),
            Usage {
                five_hour: Some(12),
                weekly: Some(32),
                week_reset: Some(1791028800),
                five_reset: None
            }
        );
    }
    #[test]
    fn rejects_missing_quota() {
        assert!(parse_usage(&json!({"rateLimits": {"primary": null}})).is_err());
        assert!(parse_usage(
            &json!({"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 15}}})
        )
        .is_err());
    }
}
