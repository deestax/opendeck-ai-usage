use base64::Engine;
use openaction::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, SystemTime};
use tokio::process::Command;
use tokio::sync::Mutex;

const LIMITS: &str = "local.superdao.claudeusage.limits";
const EXTRA: &str = "local.superdao.claudeusage.allowance";
const SUMMARY: &str = "local.superdao.claudeusage.summary";
const REFRESH: Duration = Duration::from_secs(300);
const TAP_MIN: Duration = Duration::from_secs(30);

#[derive(Clone, Default, Serialize, Deserialize)]
struct Settings {}
#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct Cache {
    data: Option<Value>,
    attempted: Option<SystemTime>,
    next: Option<SystemTime>,
    last_success: Option<SystemTime>,
    credential_source: Option<PathBuf>,
    error: Option<String>,
    rate_limited: bool,
    rate_failures: u32,
    credential_stamp: Option<(std::time::SystemTime, u64)>,
}

fn credential_source() -> Option<PathBuf> {
    let config = std::env::var_os("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        .or_else(|| std::env::var_os("CLAUDE_CONFIG_DIR"))
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".claude")))?;
    Some(
        config
            .canonicalize()
            .unwrap_or(config)
            .join(".credentials.json"),
    )
}
fn credential_stamp() -> Option<(SystemTime, u64)> {
    let metadata = std::fs::metadata(credential_source()?).ok()?;
    Some((metadata.modified().ok()?, metadata.len()))
}
fn cache_path() -> Option<PathBuf> {
    let root = std::env::var_os("XDG_CACHE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))?;
    Some(root.join("opendeck-claude-usage/state.json"))
}
fn load_cache_from(
    path: &Path,
    source: Option<PathBuf>,
    stamp: Option<(SystemTime, u64)>,
    now: SystemTime,
) -> Cache {
    let read = || -> Option<Cache> {
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .ok()?
            .take(524289)
            .read_to_end(&mut bytes)
            .ok()?;
        if bytes.len() > 524288 {
            return None;
        }
        serde_json::from_slice(&bytes).ok()
    };
    let mut cache = read().unwrap_or_default();
    if cache.credential_source != source {
        cache = Cache::default();
    }
    if cache.credential_stamp != stamp
        || stamp.is_none()
        || cache.last_success.is_none_or(|t| {
            now.duration_since(t)
                .map_or(true, |d| d > Duration::from_secs(7 * 86400))
        })
    {
        cache.data = None;
        cache.last_success = None;
    }
    // Always revalidate startup readings; never erase a still-active 429 gate.
    if !cache.rate_limited
        || cache.next.is_some_and(|t| {
            t.duration_since(now)
                .is_ok_and(|d| d > Duration::from_secs(86400))
        })
    {
        cache.next = None;
    }
    if cache.data.is_some() && cache.error.is_none() {
        cache.error = Some("Cached usage".into());
    }
    cache.credential_source = source;
    cache
}
fn save_cache_to(path: &Path, cache: &Cache) -> std::io::Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| std::io::Error::other("No cache directory"))?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)?;
    let tmp = dir.join(format!(
        ".state-{}-{}.tmp",
        std::process::id(),
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        serde_json::to_writer(&mut file, cache)?;
        file.flush()?;
        file.sync_all()?;
        std::fs::rename(&tmp, path)?;
        std::fs::File::open(dir)?.sync_all()
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(tmp);
    }
    result
}
fn elapsed(now: SystemTime, previous: SystemTime) -> Duration {
    now.duration_since(previous).unwrap_or_default()
}

fn retry_delay(rate_limited: bool, retry: u64, rate_failures: &mut u32) -> u64 {
    if rate_limited {
        *rate_failures = rate_failures.saturating_add(1);
        retry
            .clamp(900, 86400)
            .max(900 * 2_u64.pow((*rate_failures).min(3) - 1))
    } else {
        *rate_failures = 0;
        300
    }
}
static CACHE: LazyLock<Mutex<Cache>> = LazyLock::new(|| {
    Mutex::new(
        cache_path()
            .map(|p| {
                load_cache_from(
                    &p,
                    credential_source(),
                    credential_stamp(),
                    SystemTime::now(),
                )
            })
            .unwrap_or_default(),
    )
});

// Helper coordinates renewal locally; no token appears in args, settings, cache or logs.
async fn fetch() -> Result<Value, String> {
    let dir = std::env::current_exe()
        .map_err(|_| "Helper unavailable")?
        .parent()
        .ok_or("Helper unavailable")?
        .to_path_buf();
    let mut command = Command::new("python3");
    command
        .arg(dir.join("fetch_usage.py"))
        .kill_on_drop(true)
        .stderr(std::process::Stdio::null());
    let output = tokio::time::timeout(Duration::from_secs(55), command.output())
        .await
        .map_err(|_| "Usage timed out")?
        .map_err(|_| "Helper unavailable")?;
    if !output.status.success() {
        return Err("Helper unavailable".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "Invalid usage response".into())
}

fn percent(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .filter(|n| n.is_finite() && *n >= 0.0)
        .map(|n| n.min(100.0))
}
fn quotas(data: &Value) -> (Option<f64>, Option<f64>) {
    let mut five = None;
    let mut weekly = None;
    if let Some(limits) = data["limits"].as_array() {
        for limit in limits {
            match limit["kind"].as_str() {
                Some("session") => five = percent(&limit["percent"]),
                Some("weekly_all") => weekly = percent(&limit["percent"]),
                _ => {}
            }
        }
    }
    (
        five.or_else(|| percent(&data["five_hour"]["utilization"])),
        weekly.or_else(|| percent(&data["seven_day"]["utilization"])),
    )
}

#[derive(Debug, PartialEq)]
enum Allowance {
    Disabled,
    Remaining {
        amount: f64,
        currency: String,
        decimals: usize,
    },
    Unknown,
}
fn amount(v: &Value) -> Option<f64> {
    v.as_f64().filter(|n| n.is_finite() && *n >= 0.0)
}
fn currency(v: &Value) -> Option<String> {
    v.as_str()
        .filter(|s| s.len() == 3 && s.bytes().all(|c| c.is_ascii_uppercase()))
        .map(str::to_string)
}
fn allowance(data: &Value) -> Allowance {
    let spend = &data["spend"];
    if spend.is_object() {
        if spend["enabled"] == false {
            return Allowance::Disabled;
        }
        if spend["enabled"] != true {
            return Allowance::Unknown;
        }
        let used = &spend["used"];
        let limit = &spend["limit"];
        // Both amounts must have matching currency and scale; balance is never read.
        if let (Some(u), Some(l), Some(c), Some(e)) = (
            amount(&used["amount_minor"]),
            amount(&limit["amount_minor"]),
            currency(&limit["currency"]),
            limit["exponent"].as_u64(),
        ) {
            if e <= 6
                && currency(&used["currency"]).as_ref() == Some(&c)
                && used["exponent"].as_u64() == Some(e)
            {
                return Allowance::Remaining {
                    amount: (l - u).max(0.0) / 10_f64.powi(e as i32),
                    currency: c,
                    decimals: e as usize,
                };
            }
        }
        return Allowance::Unknown;
    }
    let extra = &data["extra_usage"];
    if extra["is_enabled"] == false {
        return Allowance::Disabled;
    }
    if extra["is_enabled"] != true {
        return Allowance::Unknown;
    }
    if let (Some(u), Some(l), Some(c)) = (
        amount(&extra["used_credits"]),
        amount(&extra["monthly_limit"]),
        currency(&extra["currency"]),
    ) {
        let e = extra["decimal_places"].as_u64().unwrap_or(2);
        if e <= 6 {
            return Allowance::Remaining {
                amount: (l - u).max(0.0) / 10_f64.powi(e as i32),
                currency: c,
                decimals: e as usize,
            };
        }
    }
    Allowance::Unknown
}

fn xml(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
fn svg(title: &str, main: &str, sub: &str, status: &str) -> String {
    format!(
        r##"<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144"><rect width="144" height="144" rx="8" fill="#201a18"/><rect x="0" y="0" width="144" height="5" fill="#d99676"/><text x="72" y="29" fill="#d9ad97" text-anchor="middle" font-family="sans-serif" font-size="16">{}</text><text x="72" y="72" fill="#fff0e7" text-anchor="middle" font-family="sans-serif" font-size="25">{}</text><text x="72" y="105" fill="#9fdbb8" text-anchor="middle" font-family="sans-serif" font-size="18">{}</text><text x="72" y="132" fill="#d9ad97" text-anchor="middle" font-family="sans-serif" font-size="12">{}</text></svg>"##,
        xml(title),
        xml(main),
        xml(sub),
        xml(status)
    )
}
fn image(data: Option<&Value>, error: Option<&str>, extra: bool) -> String {
    let title = if extra { "CLAUDE EXTRA" } else { "CLAUDE USED" };
    let status = if error.is_some() {
        "STALE - tap to retry"
    } else if extra {
        "monthly allowance"
    } else {
        "quota used"
    };
    let Some(data) = data else {
        return svg(
            title,
            "unavailable",
            error.unwrap_or("waiting"),
            "tap to retry",
        );
    };
    if extra {
        match allowance(data) {
            Allowance::Disabled => svg(title, "disabled", "extra usage off", status),
            Allowance::Unknown => svg(title, "--", "cap unavailable", status),
            Allowance::Remaining {
                amount,
                currency,
                decimals,
            } => {
                let symbol = match currency.as_str() {
                    "EUR" => "€",
                    "USD" => "$",
                    "GBP" => "£",
                    _ => "",
                };
                let formatted = format!("{symbol}{amount:.decimals$}");
                // Keep even large allowances readable on a small key.
                let formatted = if formatted.len() > 13 {
                    format!("{symbol}{amount:.0}")
                } else {
                    formatted
                };
                svg(title, &formatted, &format!("{currency} left"), status)
            }
        }
    } else {
        let (five, weekly) = quotas(data);
        let show = |v: Option<f64>| v.map(|p| format!("{p:.0}%")).unwrap_or_else(|| "--".into());
        svg(
            title,
            &format!("5h {}", show(five)),
            &format!("week {}", show(weekly)),
            status,
        )
    }
}

fn reset_times(data: &Value) -> (Option<i64>, Option<i64>) {
    let parse = |v: &Value| v.as_str().and_then(opendeck_style::iso);
    let mut five = None;
    let mut week = None;
    if let Some(limits) = data["limits"].as_array() {
        for limit in limits {
            match limit["kind"].as_str() {
                Some("session") => five = parse(&limit["resets_at"]),
                Some("weekly_all") => week = parse(&limit["resets_at"]),
                _ => {}
            }
        }
    }
    (
        five.or_else(|| parse(&data["five_hour"]["resets_at"])),
        week.or_else(|| parse(&data["seven_day"]["resets_at"])),
    )
}
fn summary_image(data: Option<&Value>, error: Option<&str>) -> String {
    let Some(data) = data else {
        return opendeck_style::metric(
            "CLAUDE",
            "--",
            error.unwrap_or("waiting"),
            opendeck_style::PEACH,
            None,
        );
    };
    let (five, week) = quotas(data);
    let (five_reset, week_reset) = reset_times(data);
    opendeck_style::quota(
        "CLAUDE",
        opendeck_style::PEACH,
        five,
        week,
        five_reset,
        week_reset,
        error.is_some(),
        opendeck_style::now(),
    )
}

async fn refresh(force: bool) {
    // Serializes all callers. Both actions share a fetch and error backoff.
    let mut cache = CACHE.lock().await;
    // Wall-clock deadlines include suspend time, unlike Linux CLOCK_MONOTONIC.
    let now = SystemTime::now();
    let stamp = credential_stamp();
    if stamp != cache.credential_stamp {
        // Credentials may belong to a newly signed-in account. Don't show its predecessor's usage.
        cache.data = None;
        cache.last_success = None;
        if !cache.rate_limited {
            cache.next = None;
        }
    }
    cache.credential_stamp = stamp;
    cache.credential_source = credential_source();
    let due = cache.next.is_none_or(|at| now >= at);
    let tap = force
        && !cache.rate_limited
        && cache.attempted.is_none_or(|at| elapsed(now, at) >= TAP_MIN);
    if due || tap {
        cache.attempted = Some(now);
        match fetch().await {
            Ok(result) if result["data"].is_object() => {
                cache.data = Some(result["data"].clone());
                cache.error = None;
                cache.rate_limited = false;
                cache.rate_failures = 0;
                cache.last_success = Some(SystemTime::now());
                cache.next = Some(SystemTime::now() + REFRESH);
                if result["auth_refreshed"] == true {
                    log::info!("Claude token renewed safely");
                }
                log::info!("Claude usage updated");
            }
            result => {
                let (error, rate_limited, retry) = match result {
                    Ok(value) => (
                        value["error"]
                            .as_str()
                            .unwrap_or("Usage unavailable")
                            .to_string(),
                        value["rate_limited"] == true,
                        value["retry_after"]
                            .as_u64()
                            .unwrap_or(900)
                            .clamp(900, 86400),
                    ),
                    Err(error) => (error, false, 900),
                };
                let backoff = retry_delay(rate_limited, retry, &mut cache.rate_failures);
                log::warn!("Claude usage: {error}; retry in {backoff}s");
                cache.error = Some(error);
                cache.rate_limited = rate_limited;
                cache.next = Some(SystemTime::now() + Duration::from_secs(backoff));
            }
        }
        // The helper may have renewed credentials during this fetch.
        cache.credential_stamp = credential_stamp();
        if let Some(path) = cache_path() {
            if save_cache_to(&path, &cache).is_err() {
                log::warn!("Could not save Claude usage cache");
            }
        }
    }
    let limits = summary_image(cache.data.as_ref(), cache.error.as_deref());
    let extra = image(cache.data.as_ref(), cache.error.as_deref(), true);
    let summary = summary_image(cache.data.as_ref(), cache.error.as_deref());
    drop(cache);
    for (uuid, svg) in [(LIMITS, limits), (EXTRA, extra), (SUMMARY, summary)] {
        let uri = format!(
            "data:image/svg+xml;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(svg)
        );
        for instance in visible_instances(uuid).await {
            if let Err(e) = instance.set_image(Some(uri.clone()), None).await {
                log::warn!("Claude key update failed: {e}");
            }
        }
    }
}

struct LimitsAction;
struct ExtraAction;
struct SummaryAction;
macro_rules! action {
    ($kind:ty, $uuid:expr) => {
        #[async_trait]
        impl Action for $kind {
            const UUID: ActionUuid = $uuid;
            type Settings = Settings;
            async fn will_appear(&self, _: &Instance, _: &Settings) -> OpenActionResult<()> {
                refresh(false).await;
                Ok(())
            }
            async fn key_up(&self, _: &Instance, _: &Settings) -> OpenActionResult<()> {
                refresh(true).await;
                Ok(())
            }
        }
    };
}
action!(LimitsAction, LIMITS);
action!(ExtraAction, EXTRA);
action!(SummaryAction, SUMMARY);

#[tokio::main]
async fn main() -> OpenActionResult<()> {
    if std::env::args().nth(1).as_deref() == Some("--check") {
        let value = match fetch().await {
            Ok(value) => value,
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(1);
            }
        };
        let data = value.get("data");
        println!(
            "{}",
            serde_json::json!({"limits_svg":image(data, value["error"].as_str(),false), "allowance_svg":image(data,value["error"].as_str(),true), "summary_svg":summary_image(data,value["error"].as_str())})
        );
        return Ok(());
    }
    use simplelog::*;
    let _ = TermLogger::init(
        LevelFilter::Info,
        Config::default(),
        TerminalMode::Stdout,
        ColorChoice::Never,
    );
    register_action(LimitsAction).await;
    register_action(ExtraAction).await;
    register_action(SummaryAction).await;
    let ticker = tokio::spawn(async {
        let mut interval = tokio::time::interval(Duration::from_secs(30));
        loop {
            interval.tick().await;
            if !visible_instances(LIMITS).await.is_empty()
                || !visible_instances(EXTRA).await.is_empty()
                || !visible_instances(SUMMARY).await.is_empty()
            {
                refresh(false).await;
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
    use serde_json::json;
    #[test]
    fn structured_limits_fallback_and_zero() {
        assert_eq!(
            quotas(
                &json!({"limits":[{"kind":"weekly_all","percent":0,"is_active":false},{"kind":"session","percent":32.4}]})
            ),
            (Some(32.4), Some(0.0))
        );
        assert_eq!(
            quotas(&json!({"five_hour":{"utilization":5},"seven_day":null})),
            (Some(5.0), None)
        );
        assert_eq!(quotas(&json!({})), (None, None));
    }
    fn spend(used: f64, limit: f64) -> Value {
        json!({"spend":{"enabled":true,"used":{"amount_minor":used,"currency":"EUR","exponent":2},"limit":{"amount_minor":limit,"currency":"EUR","exponent":2},"balance":{"amount_minor":999999}}})
    }
    #[test]
    fn allowance_is_cap_minus_spend_not_wallet() {
        assert_eq!(
            allowance(&spend(345.0, 2500.0)),
            Allowance::Remaining {
                amount: 21.55,
                currency: "EUR".into(),
                decimals: 2
            }
        );
        assert_eq!(
            allowance(&spend(3000.0, 2500.0)),
            Allowance::Remaining {
                amount: 0.0,
                currency: "EUR".into(),
                decimals: 2
            }
        );
        assert_eq!(
            allowance(&spend(0.0, 0.0)),
            Allowance::Remaining {
                amount: 0.0,
                currency: "EUR".into(),
                decimals: 2
            }
        );
    }
    #[test]
    fn missing_disabled_unlimited_and_currency_mismatch() {
        assert_eq!(allowance(&json!({})), Allowance::Unknown);
        assert_eq!(
            allowance(&json!({"spend":{"enabled":false}})),
            Allowance::Disabled
        );
        assert_eq!(
            allowance(&json!({"spend":{"enabled":true,"limit":null}})),
            Allowance::Unknown
        );
        let mut value = spend(0.0, 2500.0);
        value["spend"]["used"]["currency"] = json!("USD");
        assert_eq!(allowance(&value), Allowance::Unknown);
    }
    #[test]
    fn summary_shows_resets_not_allowance() {
        let mut data = spend(0.0, 2500.0);
        data["five_hour"] = json!({"utilization":4});
        data["seven_day"] = json!({"utilization":0});
        let svg = summary_image(Some(&data), None);
        assert!(svg.contains("4%"));
        assert!(svg.contains("0%"));
        assert!(!svg.contains("25.00"));
        assert!(svg.contains("reset --"));
        assert!(summary_image(Some(&data), Some("Rate limited")).contains("OLD"));
        data["limits"] = json!([{"kind":"session","resets_at":"2026-10-02T17:00:00+07:00"},{"kind":"weekly_all","resets_at":"2026-10-03T10:00:00Z"}]);
        assert_eq!(
            reset_times(&data),
            (
                opendeck_style::iso("2026-10-02T10:00:00Z"),
                opendeck_style::iso("2026-10-03T10:00:00Z")
            )
        );
        data["limits"] = json!([{"kind":"session","resets_at":"bad"}]);
        data["five_hour"]["resets_at"] = json!("2026-10-02T10:00:00Z");
        assert_eq!(
            reset_times(&data).0,
            opendeck_style::iso("2026-10-02T10:00:00Z")
        );
        assert!(
            summary_image(None, Some("Sign in to Claude Code")).contains("Sign in to Claude Code")
        );
        assert!(summary_image(Some(&json!({})), None).contains("reset --"));
    }
    #[test]
    fn auth_errors_do_not_escalate_rate_backoff() {
        let mut failures = 0;
        for _ in 0..3 {
            assert_eq!(retry_delay(false, 900, &mut failures), 300);
        }
        assert_eq!(retry_delay(true, 900, &mut failures), 900);
        assert_eq!(retry_delay(true, 900, &mut failures), 1800);
        assert_eq!(retry_delay(true, 900, &mut failures), 3600);
        assert_eq!(retry_delay(true, 7200, &mut failures), 7200);
        assert_eq!(retry_delay(false, 900, &mut failures), 300);
        assert_eq!(retry_delay(true, 900, &mut failures), 900);
    }
    #[test]
    fn persisted_cache_is_private_stale_and_account_bound() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!(
            "claude-cache-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path = dir.join("state.json");
        let now = SystemTime::now();
        let source = Some(PathBuf::from("/test/.credentials.json"));
        let stamp = Some((now, 100));
        let cache = Cache {
            data: Some(json!({"five_hour":{"utilization":12}})),
            last_success: Some(now),
            credential_source: source.clone(),
            credential_stamp: stamp,
            next: Some(now + REFRESH),
            ..Cache::default()
        };
        save_cache_to(&path, &cache).unwrap();
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let loaded = load_cache_from(&path, source.clone(), stamp, now);
        assert_eq!(loaded.data, cache.data);
        assert!(loaded.error.is_some());
        assert!(loaded.next.is_none());
        assert!(
            load_cache_from(&path, source.clone(), Some((now, 101)), now)
                .data
                .is_none()
        );
        assert!(
            load_cache_from(&path, Some(PathBuf::from("/another/login")), stamp, now)
                .data
                .is_none()
        );
        assert!(load_cache_from(
            &path,
            source.clone(),
            stamp,
            now + Duration::from_secs(8 * 86400)
        )
        .data
        .is_none());
        std::fs::write(&path, b"broken").unwrap();
        assert!(load_cache_from(&path, source, stamp, now).data.is_none());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn restart_preserves_rate_limit_and_sleep_advances_deadline() {
        let dir = std::env::temp_dir().join(format!(
            "claude-backoff-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path = dir.join("state.json");
        let now = SystemTime::now();
        let source = Some(PathBuf::from("/test/.credentials.json"));
        let stamp = Some((now, 100));
        let cache = Cache {
            rate_limited: true,
            rate_failures: 2,
            next: Some(now + Duration::from_secs(1800)),
            credential_source: source.clone(),
            credential_stamp: stamp,
            ..Cache::default()
        };
        save_cache_to(&path, &cache).unwrap();
        let loaded = load_cache_from(&path, source.clone(), stamp, now);
        assert_eq!(loaded.next, cache.next);
        assert_eq!(loaded.rate_failures, 2);
        // Credential changes also must not bypass a server's retry gate.
        assert_eq!(
            load_cache_from(&path, source, Some((now, 101)), now).next,
            cache.next
        );
        let after_sleep = now + Duration::from_secs(3600);
        assert!(after_sleep >= loaded.next.unwrap());
        assert!(elapsed(after_sleep, now) >= TAP_MIN);
        assert_eq!(elapsed(now, after_sleep), Duration::ZERO);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn legacy_money_and_stale_error() {
        assert_eq!(
            allowance(
                &json!({"extra_usage":{"is_enabled":true,"used_credits":500,"monthly_limit":2500,"currency":"EUR","decimal_places":2}})
            ),
            Allowance::Remaining {
                amount: 20.0,
                currency: "EUR".into(),
                decimals: 2
            }
        );
        assert!(image(Some(&spend(0.0, 2500.0)), Some("Rate limited"), true).contains("STALE"));
        assert!(image(None, Some("Sign in to Claude Code"), false).contains("unavailable"));
        assert_eq!(xml("<&"), "&lt;&amp;");
    }
}
