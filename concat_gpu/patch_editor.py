"""Patch pinned upstream Concat v0.2.8's OWN Rust/Slint File menu.

No separate editor. Fail closed on upstream drift; don't edit the source lock.
"""
from __future__ import annotations
import argparse
import shutil
from pathlib import Path

PIN = "79116bd0f9a103c633d888603ad27890e929dbdc"

def replace_once(text: str, original: str, revised: str, label: str) -> str:
    found = text.count(original)
    if found != 1:
        raise RuntimeError(f"{label}: expected unique upstream anchor, saw {found}")
    return text.replace(original, revised, 1)

def patch(root: Path) -> None:
    lib_path = root / "src/crates/concat/src/lib.rs"
    studio_path = root / "src/crates/concat/src/studio.rs"
    ps_path = root / "src/crates/concat/src/concat_gpu_client.ps1"
    root = root.resolve(strict=True)
    if not (root / "src/Cargo.lock").is_file():
        raise RuntimeError("Expected original upstream Concat checkout")
    lib = lib_path.read_text(encoding="utf-8")
    studio = studio_path.read_text(encoding="utf-8")
    lib = replace_once(lib, "mod gpu;\n", "mod gpu;\n#[cfg(windows)]\nmod cloud_gpu;\n", "module")
    lib = replace_once(lib,
        '                        "export" => state.handle(Msg::Export(ExportMsg::Open)),\n',
        '                        "export" => state.handle(Msg::Export(ExportMsg::Open)),\n'
        '                        #[cfg(windows)]\n'
        '                        "send-gpu" => state.send_to_gpu(),\n',
        "native File menu handler")
    studio = replace_once(studio,
        '                row(\n                    "template",\n',
        '                #[cfg(windows)]\n'
        '                row(\n'
        '                    "send-gpu",\n'
        '                    "Send to GPU (CUDA)...".to_string(),\n'
        '                    Glyph::Export,\n'
        '                    "",\n'
        '                    self.session.is_some(),\n'
        '                ),\n'
        '                row(\n                    "template",\n',
        "native File menu row")
    new_method = """    /// Save the live Concat edit before handing its unchanged project to
    /// the GPU uploader. Existing local export and editing paths remain intact.
    #[cfg(windows)]
    pub fn send_to_gpu(&mut self) {
        let Some(session) = self.session.as_mut() else {
            self.notify("Open a Concat project before sending it to GPU", true);
            return;
        };
        // save_ordered uses the upstream atomic write + ticket ordering.
        // A screenshot of a preview is never a substitute for the saved edit.
        let (path, document) = session.prepare_save(None);
        self.autosave.stop();
        let ticket = projects::ticket();
        match projects::save_ordered(&path, &document, ticket) {
            Ok(_) => {
                self.dirty = false;
                match crate::cloud_gpu::launch(&path) {
                    Ok(()) => self.notify("GPU upload opened; follow progress in the new window", false),
                    Err(error) => self.notify(&format!("Could not start GPU upload: {error}"), true),
                }
            }
            Err(error) => {
                self.dirty = true;
                self.notify(&format!("GPU upload blocked; Concat save failed: {error}"), true);
            }
        }
    }

"""
    studio = replace_once(studio,
        "    /// The audible clip set, handed to playback whenever the edit changes.\n",
        new_method + "    /// The audible clip set, handed to playback whenever the edit changes.\n",
        "save-then-send method")
    lib_path.write_text(lib, encoding="utf-8")
    studio_path.write_text(studio, encoding="utf-8")
    helper = Path(__file__).with_name("cloud_gpu.rs").read_bytes()
    rust_path = root / "src/crates/concat/src/cloud_gpu.rs"
    if rust_path.exists() or ps_path.exists():
        raise RuntimeError("Refusing to overwrite upstream cloud GPU files")
    rust_path.write_bytes(helper)
    shutil.copy2(Path(__file__).with_name("concat_gpu_client.ps1"), ps_path)
    print("Patched native Concat File menu and embedded secure GPU client")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    args = parser.parse_args()
    patch(args.upstream)
