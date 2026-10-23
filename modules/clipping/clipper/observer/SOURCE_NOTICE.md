# Source notice

`bilibili.py`, `media.py`, and `audio.py` were copied from AI同担 (`BBBBchan/AI-Tongdan`, revision d8015db), from its `app/` package.

Source: https://github.com/BBBBchan/AI-Tongdan
License: Creative Commons Attribution-NonCommercial 4.0 International.
Original copyright and attribution remain with the upstream project authors.
The local source project's LICENSE applies to these derived modules.

Stream Clipper changes `media.py` to retain the HLS archive and bound the
audio decoder's segment-index memory. `bilibili.py` only reads rooms, streams,
and audience chat; no messaging, following, or other interaction module is
included. Audio speaker tracking stays conservative; it does not identify
the host automatically.
