use base64::Engine;
use std::time::{SystemTime, UNIX_EPOCH};
pub const MINT: &str = "#72e2c1";
pub const PEACH: &str = "#f2ad89";
pub const BLUE: &str = "#8fbaff";
pub const LILAC: &str = "#bea9ff";
pub fn xml(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}
pub fn iso(s: &str) -> Option<i64> {
    time::OffsetDateTime::parse(s, &time::format_description::well_known::Rfc3339)
        .ok()
        .map(|d| d.unix_timestamp())
}
pub fn relative(reset: Option<i64>, now: i64) -> String {
    let Some(at) = reset.filter(|at| *at > 0) else {
        return "reset --".into();
    };
    let left = at.saturating_sub(now);
    if left <= 0 {
        return "reset due".into();
    }
    if left < 60 {
        return "in <1m".into();
    }
    let m = left / 60;
    if m >= 1440 {
        format!("in {}d {}h", m / 1440, m % 1440 / 60)
    } else if m >= 60 {
        format!("in {}h {}m", m / 60, m % 60)
    } else {
        format!("in {m}m")
    }
}
pub fn text(x: u16, y: u16, size: u16, color: &str, anchor: &str, value: &str) -> String {
    format!(
        r#"<text x="{x}" y="{y}" fill="{color}" text-anchor="{anchor}" font-family="DejaVu Sans,sans-serif" font-size="{size}">{}</text>"#,
        xml(value)
    )
}
pub fn card(title: &str, accent: &str, body: &str) -> String {
    format!(
        r##"<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144"><defs><linearGradient id="bg" x2="0.8" y2="1"><stop stop-color="#202c40"/><stop offset="1" stop-color="#0c1421"/></linearGradient></defs><rect width="144" height="144" fill="#080e18"/><rect x="3" y="3" width="138" height="138" rx="17" fill="url(#bg)" stroke="#344257"/><rect x="15" y="14" width="4" height="14" rx="2" fill="{accent}"/>{}{body}</svg>"##,
        text(26, 26, 13, accent, "start", title)
    )
}
pub fn bar(y: u16, pct: Option<f64>, accent: &str) -> String {
    let w = pct
        .filter(|n| n.is_finite())
        .unwrap_or(0.0)
        .clamp(0.0, 100.0)
        * 1.12;
    let color = if pct.is_some_and(|p| p >= 90.0) {
        "#ff9b9b"
    } else {
        accent
    };
    format!(
        r##"<rect x="16" y="{y}" width="112" height="4" rx="2" fill="#344257"/><rect x="16" y="{y}" width="{w:.1}" height="4" rx="2" fill="{color}"/>"##
    )
}
#[allow(clippy::too_many_arguments)]
pub fn quota(
    title: &str,
    accent: &str,
    five: Option<f64>,
    week: Option<f64>,
    five_reset: Option<i64>,
    week_reset: Option<i64>,
    stale: bool,
    at: i64,
) -> String {
    if let (None, Some(weekly)) = (five, week) {
        let heading = if stale {
            format!("{title} · OLD")
        } else {
            title.into()
        };
        return card(
            &heading,
            accent,
            &format!(
                "{}{}{}{}{}",
                text(16, 48, 11, "#a8b6cb", "start", "WEEKLY USED"),
                text(72, 84, 34, "#eef4ff", "middle", &format!("{weekly:.0}%")),
                bar(95, week, accent),
                text(16, 116, 10, "#a8b6cb", "start", "RESETS"),
                text(128, 132, 13, accent, "end", &relative(week_reset, at))
            ),
        );
    }
    let mut body = String::new();
    for (label, pct, reset, y) in [
        ("5H", five, five_reset, 48),
        ("WEEK", week, week_reset, 101),
    ] {
        let value = pct
            .map(|p| format!("{p:.0}%"))
            .unwrap_or_else(|| "--".into());
        body += &text(16, y, 12, "#a8b6cb", "start", label);
        body += &text(128, y, 20, "#eef4ff", "end", &value);
        body += &bar(y + 7, pct, accent);
        body += &text(16, y + 25, 12, "#a8b6cb", "start", &relative(reset, at));
    }
    let heading = if stale {
        format!("{title} · OLD")
    } else {
        format!("{title} · USED")
    };
    card(&heading, accent, &body)
}
pub fn metric(title: &str, value: &str, sub: &str, accent: &str, pct: Option<f64>) -> String {
    let size = if value.chars().count() > 9 { 19 } else { 28 };
    card(
        title,
        accent,
        &format!(
            "{}{}{}",
            text(72, 79, size, "#eef4ff", "middle", value),
            text(72, 104, 12, "#a8b6cb", "middle", sub),
            bar(119, pct, accent)
        ),
    )
}
pub fn control(kind: &str, art: Option<&str>) -> String {
    let (title, shape) = match kind {
        "previous" => (
            "PREVIOUS",
            r#"<path d="M83 55 L57 73 L83 91Z"/><rect x="48" y="55" width="5" height="36" rx="2"/>"#,
        ),
        "next" => (
            "NEXT",
            r#"<path d="M61 55 L87 73 L61 91Z"/><rect x="91" y="55" width="5" height="36" rx="2"/>"#,
        ),
        "profiles" => (
            "PROFILES",
            r#"<rect x="48" y="51" width="20" height="20" rx="4"/><rect x="76" y="51" width="20" height="20" rx="4"/><rect x="48" y="79" width="20" height="20" rx="4"/><rect x="76" y="79" width="20" height="20" rx="4"/>"#,
        ),
        _ => (
            "PLAY / PAUSE",
            r#"<path d="M48 55 L72 73 L48 91Z"/><rect x="80" y="55" width="6" height="36" rx="2"/><rect x="91" y="55" width="6" height="36" rx="2"/>"#,
        ),
    };
    let art=art.filter(|a|a.starts_with("data:image/jpeg;base64,")||a.starts_with("data:image/png;base64,"))
        .map(|a|format!(r##"<defs><clipPath id="art"><rect x="15" y="38" width="114" height="91" rx="12"/></clipPath></defs><image x="15" y="38" width="114" height="91" preserveAspectRatio="xMidYMid slice" clip-path="url(#art)" href="{}"/><rect x="15" y="38" width="114" height="91" rx="12" fill="#0c1421" opacity="0.7"/>"##,xml(a))).unwrap_or_default();
    card(
        title,
        BLUE,
        &format!(
            r##"{art}<g fill="#dceaff">{shape}</g>{}"##,
            text(
                72,
                121,
                11,
                "#a8b6cb",
                "middle",
                if kind == "profiles" {
                    "SWITCH PAGE"
                } else {
                    "MEDIA"
                }
            )
        ),
    )
}
pub fn uri(svg: &str) -> String {
    format!(
        "data:image/svg+xml;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(svg)
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn relative_boundaries() {
        for (d, s) in [
            (0, "reset due"),
            (-100, "reset due"),
            (1, "in <1m"),
            (59, "in <1m"),
            (60, "in 1m"),
            (3600, "in 1h 0m"),
            (90061, "in 1d 1h"),
        ] {
            assert_eq!(relative(Some(100000 + d), 100000), s);
        }
        assert_eq!(relative(None, 0), "reset --");
        assert_eq!(relative(Some(0), 0), "reset --");
    }
    #[test]
    fn timezone() {
        assert_eq!(
            iso("2026-10-02T17:00:00+07:00"),
            iso("2026-10-02T10:00:00Z")
        );
        assert!(iso("bad").is_none());
    }
    #[test]
    fn escaping() {
        assert_eq!(xml("<&\""), "&lt;&amp;&quot;");
    }
}
