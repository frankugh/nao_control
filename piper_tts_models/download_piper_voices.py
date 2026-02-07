from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="rhasspy/piper-voices",
    allow_patterns=["nl/*.onnx", "nl/*.onnx.json"],
    local_dir=""
)
print("Klaar.")
