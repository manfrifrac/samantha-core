# Creative & Multimedia Studio — Production Pipeline Tracker

## 🎬 Active Media Pipelines

| ID | Project Name | Media Type | Status | Lead Exec | Output Artifact |
|---|---|---|---|---|---|
| #301 | 3D Audio-Reactive WebGL Mesh | Interactive HTML5/GLSL | 🟢 Complete | `exec_mesh_01` | `static/mapping/dodecahedron.html` |
| #302 | Mobile Optimized Trailer Clip | MP4 (H.264 / AAC) | 🟢 Complete | `exec_ffmpeg_02` | `/tmp/docs/clip_faststart.mp4` |
| #303 | Wikimedia Stock Footage Sweep | HD Ambient Ocean Loops | 🟡 Active | `exec_wiki_03` | `/tmp/docs/stock_manifest.json` |

---

## 📌 Media Standards
- Audio codec: Ogg/Opus (mono, 24kHz) for messaging; AAC (128k stereo) for video containers.
- Video container: MP4 with `moov` atom at file beginning (`-movflags +faststart`).
- Pixel format: Strict `yuv420p` for cross-platform iOS and Android hardware acceleration.
