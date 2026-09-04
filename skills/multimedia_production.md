# Global Skills — Media, Graphics & 3D Production (`multimedia`)

> **Domain**: Headless media pipelines, FFmpeg video processing and transcoding, audio-reactive 3D WebGL (Three.js), procedural vector design (SVG), open asset ingestion, and multimodal AI pipelines.  
> **Source Reference**: Production implementations across audio/video workstations, visual mapping setups, and autonomous multimedia creators.

---

## 1. Audio-Reactive 3D Video Mapping & WebGL Simulations (Three.js + Web Audio API)

- **Problem Solved**:
  Projecting real-time generative visual mappings synchronized to audio BPM and frequency bands without requiring heavyweight proprietary VJ desktop software (e.g. Resolume, MadMapper) or dedicated server GPUs.
- **Technical Explanation**:
  A lightweight standalone HTML5/WebGL application using Three.js and the Web Audio API analyzes incoming audio streams in real-time via an `AnalyserNode`. Calling `getByteFrequencyData()` samples audio spectra per frame (`requestAnimationFrame`), mapping frequencies to geometric and spatial parameters:
  - **Sub-Bass (20–60 Hz)**: Modulates camera zoom and physical mesh scale pulsation.
  - **Mid-Range (250–2000 Hz)**: Drives rotation, geometric displacement of wireframe lattices, and mathematical particle systems.
  - **Treble / High Frequencies (4–16 kHz)**: Triggers dynamic particle emissions and color palette transitions.
- **Implementation Guide**:
  1. Serve static web applications through Nginx with zero heavy build toolchain overhead.
  2. Provide real-time parameter controls (BPM tap, wireframe toggles, color blending modes) via lightweight HTTP APIs.
  3. Keep Three.js script bundles minimal and locally cached.

---

## 2. Fast-Seek & Mobile-Optimized Video Encoding via FFmpeg

- **Problem Solved**:
  Extracting high-definition clips from large multi-gigabyte source videos ensuring immediate streaming playback on mobile devices and Telegram without audio/video desynchronization.
- **Technical Explanation**:
  Positioning `-ss` *before* `-i` enables fast seeking directly to the nearest keyframe without decoding prior frames. Enforcing the `yuv420p` pixel format guarantees hardware decoder compatibility on iOS and Android. Setting `-movflags +faststart` shifts the `moov` atom header to the beginning of the file, enabling streaming before download completion.
- **Implementation Guide**:
  ```bash
  ffmpeg -ss 00:21:36 -to 00:32:11 -i source_video.mp4 \
      -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
      -c:a aac -b:a 128k \
      -movflags +faststart \
      output_clip_optimized.mp4
  ```
  Validate stream encoding parameters:
  ```bash
  ffprobe -v error -show_entries format=duration:stream=codec_name,bit_rate -of json output_clip_optimized.mp4
  ```

---

## 3. High-Speed Stock Asset Ingestion via Wikimedia Commons API

- **Problem Solved**:
  Downloading ambient video footage or background loops (e.g. ocean waves, cloud timelapses) from commercial stock platforms failing due to bot mitigation barriers and bandwidth throttling.
- **Technical Explanation**:
  Wikimedia Commons exposes an open public REST API offering high-resolution Creative Commons / Public Domain MP4 and WebM video files downloadable directly via standard HTTP GET requests.
- **Implementation Guide**:
  1. Query the search API endpoint:
     ```text
     https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch=filetype:video+waves&gsrnamespace=6&prop=imageinfo&iiprop=url|mime
     ```
  2. Parse the high-resolution file URL (`imageinfo[0].url`).
  3. Fetch the asset directly with `requests` or `curl` using an authentic descriptive `User-Agent`.

---

## 4. Computer Vision Video Timelapse Pipelines with HUD Overlays

- **Problem Solved**:
  Synthesizing dynamic video demonstrations from fixed image sequences (sensor feeds, camera traps, periodic web captures) with telemetry data and bounding box analytics.
- **Technical Explanation**:
  The pipeline couples computer vision object detection inference (OpenCV / PyTorch) with glassmorphic Head-Up Display (HUD) overlay rendering, compiling the annotated frame sequence into an H.264 MP4 container at 15–20 FPS.
- **Implementation Guide**:
  1. Collect timestamped sequential frames (`frame_0001.png` .. `frame_NNNN.png`).
  2. Overlay bounding boxes and telemetry panels (timestamps, object counters, FPS metrics).
  3. Compile into final optimized MP4:
     ```bash
     ffmpeg -framerate 20 -i frame_%04d.png -c:v libx264 -pix_fmt yuv420p -crf 20 video_telemetry_hud.mp4
     ```

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Gemini 2.5 Multimodal (`agy` CLI)**
  - Native visual comprehension, image generation toolchains, and spatial visual layout reasoning.
- **Secondary Engine**: **Claude 3.7 Sonnet / Opus (`claude` CLI)**
  - Mathematical precision for WebGL/Three.js shader scripts, matrix transformations, and FFmpeg filter graphs.
