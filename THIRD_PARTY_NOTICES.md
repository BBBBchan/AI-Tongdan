# Third-party notices and project provenance

AI同担 is maintained by **BBBBchan**. The application and documentation contributed by this project are licensed under **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)**, as provided in the root [LICENSE](LICENSE). Commercial use of that material is not permitted under this license.

Third-party components retain their own attribution and licenses. The project's noncommercial license does not replace the licenses of independently licensed components, dependencies or model weights.

The source is available for noncommercial use. CC BY-NC 4.0 includes a noncommercial restriction and is not an OSI-approved open-source license; see the [Open Source Definition](https://opensource.org/osd).

## DeltaStreamer

- Source: [BBBBchan/DeltaStreamer](https://github.com/BBBBchan/DeltaStreamer).
- Availability: the upstream repository is public.
- Project page: <https://bbbbchan.github.io/DeltaStreamer/>.
- Source commit: `b60625d8f14f7622c504b642178ec8be07a00e4d`.
- License: [CC BY-NC 4.0](vendor/deltastreamer/LICENSE).

The following files were copied unchanged. Original relative paths and SHA256 checksums are recorded in [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json).

| Upstream path | Included path |
| --- | --- |
| `core/tools/base.py` | `vendor/deltastreamer/base.py` |
| `core/tools/memory.py` | `vendor/deltastreamer/memory.py` |
| `core/tools/memory_graph.py` | `vendor/deltastreamer/memory_graph.py` |

AI同担 adds the Bilibili connection, continuous asynchronous observations and repeated Q&A, audio processing and manual source labels, time-aligned memory updates, danmaku suggestions and sending, browser UI, persistence, deployment scripts and tests. These application extensions are distinct from the three unchanged upstream memory files.

The paper **[DeltaStreamer: Towards Human-Like Agentic Streaming Video Understanding](https://arxiv.org/abs/2610.19849)** is by Boyuan Sun, Deshui Miao, Wenzhao Gao, Shaoyong Jia, Shaohui Jiao, Chi Liu, Yan Shu, Derek Li, Bryan Dai, Shengsheng Qian and Qibin Hou (2026), arXiv:2610.19849. Its complete citation is at the bottom of [README.md](README.md#citation). Paper authorship and upstream attribution are independent of this repository's Git contribution history.

## HLS.js

- Source: [video-dev/hls.js](https://github.com/video-dev/hls.js), version 1.6.13.
- Included browser distribution: `static/vendor/hls.min.js`.
- Original notices: [static/vendor/hls.LICENSE](static/vendor/hls.LICENSE).
- Complete Apache License 2.0 text: [static/vendor/APACHE-2.0.txt](static/vendor/APACHE-2.0.txt).

The original notices include Copyright (c) 2017 Dailymotion and Copyright (c) 2013–2015 Brightcove for the identified derived portions. Those notices are preserved.

## CAM++ / 3D-Speaker

- Source: [modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker).
- Source revision: `065629c313eaf1a01c65c640c46d77e61e9607b4`.
- Included model implementation: `deploy/audio/vendor/speakerlab/models/campplus/`.
- License: [Apache License 2.0](deploy/audio/vendor/LICENSE).
- Provenance and SHA256 checksums: [deploy/audio/vendor/SOURCE.json](deploy/audio/vendor/SOURCE.json).

Original copyright headers are preserved in the copied model source. Small package initializer files permit importing this source subset. CAM++ weights are downloaded separately and are not included in this Git repository.

## Dependencies and model weights

Python dependencies are installed separately using the requirement files. FFmpeg is installed by the deployer; it is not bundled. Each dependency retains its own license.

The model download registry in [deploy/models.json](deploy/models.json) pins the vision, ASR and speaker model snapshots. Model weights are not distributed in this repository. Consult the corresponding official model repositories and their license files for model terms:

- [Qwen/Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8)
- [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B)
- [funasr/campplus](https://huggingface.co/funasr/campplus)

Silero VAD is installed through the `silero-vad` dependency; see [snakers4/silero-vad](https://github.com/snakers4/silero-vad) for its original license and notices. Live video, audio and audience messages belong to their respective rights holders and are not licensed by this project's source-code license.

## Integrated clipping tools

The `modules/clipping/` implementation was developed as a separate local workbench and is now distributed with AI-Tongdan under the same CC BY-NC 4.0 license. Its observer code derives from AI-Tongdan revision `d8015db`; see [the source notice](modules/clipping/clipper/observer/SOURCE_NOTICE.md). No production recordings, account cookies or private cluster deployment files are included.

- Noto Sans CJK SC Bold: SIL Open Font License, downloaded by the asset setup script from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk); its OFL notice accompanies the font.
- Clipping playback uses the repository's hls.js distribution and Apache notices described above.
- `imageio-ffmpeg` supplies a separately installed FFmpeg build, potentially including GPL components; consult [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) for build/source information.
- Optional Chinese OCR uses `rapidocr-onnxruntime` (Apache-2.0), from [RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR).
- Strong subtitle recognition and timing use [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) and [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B), whose model repositories specify Apache-2.0. Download revisions are in `deploy/models.json`.
- The local gallery uses React, React DOM, Vite and Tailwind CSS (MIT) and Lucide icons (ISC). Dependencies and reproducible source are in `modules/clipping/local-library/`; bundled license notices are retained with the build.
- Short reference titles retain their original source URLs in `reference_titles.json`; they are examples for editorial style, not this project's own videos.
