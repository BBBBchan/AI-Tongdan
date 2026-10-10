# Third-party notices and project provenance

AI同担 is maintained by **BBBBchan**. The application and documentation contributed by this project are licensed under **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)**, as provided in the root [LICENSE](LICENSE). Commercial use of that material is not permitted under this license.

Third-party components retain their own attribution and licenses. The project's noncommercial license does not replace the licenses of independently licensed components, dependencies or model weights.

## DeltaStreamer

- Source: [BBBBchan/DeltaStreamer](https://github.com/BBBBchan/DeltaStreamer).
- Availability: the upstream repository is currently private; a public release is planned.
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

The paper **DeltaStreamer: Towards Human-Like Agentic Streaming Video Understanding** is by Boyuan Sun, Deshui Miao, Wenzhao Gao, Shaoyong Jia, Shaohui Jiao, Chi Liu, Yan Shu, Derek Li, Bryan Dai, Shengsheng Qian and Qibin Hou (2026). Its complete citation is at the bottom of [README.md](README.md#citation). Paper authorship and upstream attribution are independent of this repository's Git contribution history.

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
