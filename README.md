# Concat GPU development

The native Windows editor is based on Concat v0.2.8, pinned to [79116bd](https://github.com/jub0t/Concat/commit/79116bd0f9a103c633d888603ad27890e929dbdc). Its editable source, Cargo lockfile, licence and notices are in `upstream/`. The Windows workflow compiles and smoke-tests the edited Rust/Slint application. Download the development bundle from the run's artifacts.

The native File menu's **Send to GPU (CUDA)** action saves the project and starts the existing Salad upload client. The editor and GPU worker share bundle version 1. GPU execution requires a CUDA-capable runner with NVDEC and NVENC.

The upstream editor retains its AGPL-3.0-or-later licence and third-party notices.
