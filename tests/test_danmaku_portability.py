"""The shipped writing rules must work without any private source recordings."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def portable_app(tmp_path):
    """Copy Python source only, never the user's recordings or runtime data."""
    destination = tmp_path / "portable"
    for source in (ROOT / "app").rglob("*.py"):
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    assert not (destination / "danmakus_style").exists()
    assert not list(destination.rglob("*.json"))
    return destination


@pytest.mark.parametrize("style_value", [0, 30, 60, 80, 100])
def test_candidate_generation_works_in_source_only_isolated_copy(portable_app, style_value):
    # -I excludes the checkout, PYTHONPATH and user site-packages. -S also removes
    # installed packages: the candidate rules and generation contract need only
    # the standard library and the caller's model interface.
    program = textwrap.dedent(
        """\
        import asyncio
        import json
        from pathlib import Path
        import sys

        destination = Path(sys.argv[1]).resolve()
        style_value = int(sys.argv[2])
        sys.path.insert(0, str(destination))
        assert not (destination / "danmakus_style").exists()
        assert not (destination / "data").exists()
        assert not (destination / "models").exists()
        assert not (destination / ".env").exists()
        assert not list(destination.rglob("*.json"))

        def forbid_private_inputs_and_network(event, arguments):
            if event in {"open", "os.listdir", "os.scandir"} and arguments:
                path = arguments[0]
                if isinstance(path, (str, bytes)):
                    path = Path(path.decode() if isinstance(path, bytes) else path)
                    assert "danmakus_style" not in path.parts, "Private corpus access"
                    assert path.suffix not in {".json", ".jsonl"}, "JSON file access"
                    assert not path.name.startswith(".env"), "Environment file access"
            if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
                raise AssertionError("Offline portability test attempted networking")

        sys.addaudithook(forbid_private_inputs_and_network)
        from app import danmaku_candidates as candidates
        from app import danmaku_voice

        assert Path(candidates.__file__).resolve().is_relative_to(destination)
        assert Path(danmaku_voice.__file__).resolve().is_relative_to(destination)
        rules = danmaku_voice.voice_instruction(style_value)
        assert isinstance(rules, str) and rules.strip()

        expected = ["举杯了哈哈", "蓝杯子抢镜", "跟着举杯", "这杯子挺大", "还真举起来了"]

        class Model:
            def __init__(self):
                self.calls = []

            async def complete(self, messages, **options):
                self.calls.append((messages, options))
                return json.dumps({"candidates": expected}, ensure_ascii=False)

        snapshot = {
            "observer": {"last_timestamp": 98},
            "observations": [{"kind": "observation", "window_start": 95,
                              "window_end": 98, "description": "画面中举起蓝色杯子"}],
            "audio": {"segments": [{"start": 96, "end": 98, "text": "举杯",
                                    "role": "unknown"}]},
            "danmaku": [{"timestamp": 99, "text": "好大一杯"}],
            "danmaku_history": [{"timestamp": 40, "text": "之前在喝水"}],
            "danmaku_style": {"value": style_value},
        }

        async def check():
            model = Model()
            context = candidates.candidate_context(snapshot, source_time=100)
            result = await candidates.generate_candidates(model, context)
            assert result == expected and len(result) == 5
            assert len(model.calls) == 1
            messages, options = model.calls[0]
            assert [message["role"] for message in messages] == ["system", "user"]
            assert rules in messages[0]["content"]
            assert rules not in messages[1]["content"]
            assert json.loads(messages[1]["content"])["style_value"] == style_value
            assert options["json_mode"] is True

            # Prove the shipped rules provider is actually used by generation,
            # without depending on any particular style-rule wording.
            requested_styles = []
            marker = "PORTABLE_STYLE_RULE_PROVIDER"

            def replacement(value):
                requested_styles.append(value)
                return marker

            candidates.voice_instruction = replacement
            assert await candidates.generate_candidates(model, context) == expected
            assert requested_styles == [style_value]
            assert marker in model.calls[-1][0][0]["content"]
            assert marker not in model.calls[-1][0][1]["content"]

            # Old chat history plus a built-in style is not evidence that a new
            # live event happened. This must not spend another model request.
            stale = candidates.candidate_context(snapshot, source_time=200)
            assert stale["audience_history"]
            assert not any(stale[key] for key in (
                "visual", "audio_transcripts", "audience_comments"))
            assert await candidates.generate_candidates(model, stale) == []
            assert len(model.calls) == 2
            assert requested_styles == [style_value]

        asyncio.run(check())
        print(json.dumps({"style_value": style_value, "candidate_count": 5,
                          "model_calls": 2, "private_inputs": False}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(portable_app), str(style_value)],
        cwd=portable_app,
        # No credentials, model endpoints, or configuration from this checkout.
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "style_value": style_value,
        "candidate_count": 5,
        "model_calls": 2,
        "private_inputs": False,
    }


def test_private_danmaku_and_superchat_files_are_ignored_and_untracked():
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("Git checkout required to check private-data exclusion")
    # Hypothetical names test directory coverage without opening, enumerating,
    # creating or changing any user recording.
    paths = [
        "danmakus_style/danmaku_001.json",
        "danmakus_style/superchat/messages.json",
        "danmakus_style/SC/nested/messages.json",
        "danmakus_style/notes.txt",
        "danmakus_style/.private-cache",
    ]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths) + "\n",
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert ignored.stdout.splitlines() == paths
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "danmakus_style"],
        cwd=ROOT,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert tracked.returncode == 0
    assert tracked.stdout == b"", "Private source recordings must not be tracked"
