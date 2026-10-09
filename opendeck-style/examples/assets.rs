use opendeck_style::*;
fn main() {
    let dir = std::path::Path::new("assets");
    std::fs::create_dir_all(dir).unwrap();
    let cards = [
        metric("OPENROUTER", "--", "CONNECTING", LILAC, None),
        metric("CODEX", "--", "CONNECTING", MINT, None),
        metric("CLAUDE", "--", "CONNECTING", PEACH, None),
        metric("CPU", "--", "CONNECTING", BLUE, None),
        metric("MEMORY", "--", "CONNECTING", LILAC, None),
        metric("UPTIME", "--", "CONNECTING", MINT, None),
        control("previous", None),
        control("playpause", None),
        control("next", None),
        control("profiles", None),
    ];
    for (i, svg) in cards.iter().enumerate() {
        std::fs::write(dir.join(format!("{i}.svg")), svg).unwrap()
    }
}
