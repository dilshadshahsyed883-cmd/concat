fn main() {
    let ui = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../upstream/src/crates/concat/ui/app.slint");
    let check = std::thread::Builder::new().stack_size(512 << 20).spawn(move || {
        slint_build::compile(ui)
    }).expect("Slint check thread");
    check.join().expect("Slint check panicked").expect("Slint UI compilation failed");
}
