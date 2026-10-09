use opendeck_style::*;
fn main() {
    let at = now();
    let cards = [
        metric("OPENROUTER", "$14.40", "CREDITS LEFT", LILAC, None),
        quota(
            "CODEX",
            MINT,
            None,
            Some(10.0),
            None,
            Some(at + 5 * 86400 + 3600),
            false,
            at,
        ),
        quota(
            "CLAUDE",
            PEACH,
            Some(8.0),
            Some(1.0),
            Some(at + 3 * 3600 + 23 * 60),
            Some(at + 18 * 3600 + 4 * 60),
            false,
            at,
        ),
        metric("CPU", "16%", "PROCESSOR", BLUE, Some(16.0)),
        metric("MEMORY", "17.3GB", "RAM IN USE", LILAC, None),
        card(
            "UPTIME",
            MINT,
            &format!(
                "{}{}{}",
                text(72, 70, 23, "#eef4ff", "middle", "0d 04h"),
                text(72, 99, 18, "#a8b6cb", "middle", "13m 53s"),
                text(72, 126, 11, MINT, "middle", "SYSTEM ONLINE")
            ),
        ),
        control("previous", None),
        control("playpause", None),
        control("next", None),
        control("profiles", None),
    ];
    let dir = std::path::Path::new("/tmp/opendeck-style-preview");
    std::fs::create_dir_all(dir).unwrap();
    for (i, svg) in cards.iter().enumerate() {
        std::fs::write(dir.join(format!("{i}.svg")), svg).unwrap();
    }
}
